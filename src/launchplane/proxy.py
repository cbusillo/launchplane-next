import json
import re
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from importlib.resources import files
from pathlib import PurePosixPath
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from launchplane.container_host import FileRead, HelperContainer
from launchplane.docker_answers import CommandResult, observing
from launchplane.gate import Answer, Gate
from launchplane.instance import BACK_SOON, LANE_NETWORK, Conflicting, LoadedRoute, ProxySeen, RouteStatus, Unread, WrittenRoute

PROXY_NAME = "launchplane-proxy"
# The routes the proxy loads and the gate's folder lie beside each other in the one volume: the gate renames within one
# file system, and a reading of the routes carries nothing the gate keeps.
VOLUME_FOLDER = PurePosixPath("/etc/traefik/launchplane")
ROUTES_FOLDER = VOLUME_FOLDER / "routes"
ENTRY_POINT = "web"
INSTANCE_PORT = 80
# Traefik watches no folder that is missing when it starts, and a new volume has none.
ROUTES_FOLDER_THEN_TRAEFIK = ["sh", "-c", f'mkdir -p {ROUTES_FOLDER} && exec traefik "$@"', "sh"]
PROXY = HelperContainer(
    container=PROXY_NAME,
    image="traefik:v3.7.18",
    command=[
        *ROUTES_FOLDER_THEN_TRAEFIK,
        f"--entrypoints.{ENTRY_POINT}.address=:{INSTANCE_PORT}",
        "--entrypoints.traefik.address=127.0.0.1:8080",
        "--api.insecure=true",
        f"--providers.file.directory={ROUTES_FOLDER}",
        "--providers.file.watch=true",
    ],
    ports={80: 80},
    volumes={"launchplane-proxy-routes": str(VOLUME_FOLDER)},
    quiet_networks=[LANE_NETWORK],
    outside_network="launchplane-proxy-outside",
)
RETRY_AFTER_SECONDS = 60
BACK_SOON_PAGE = HelperContainer(
    container=BACK_SOON,
    image="caddy:2.11.9-alpine",
    command=[
        "caddy",
        "respond",
        f"--listen=:{INSTANCE_PORT}",
        "--status=503",
        f"--header=Retry-After: {RETRY_AFTER_SECONDS}",
        "--header=Cache-Control: no-store",
        "--header=Content-Type: text/html; charset=utf-8",
        f"--body={files('launchplane').joinpath('back_soon.html').read_text()}",
    ],
    quiet_networks=[LANE_NETWORK],
)
LOADED_ROUTES_URL = "http://127.0.0.1:8080/api/rawdata"
FROM_ROUTES_FILE = "@file"
HOST_RULE = re.compile(r"Host\(`(?P<web_address>[^`]+)`\)")
INSTANCE_URL = re.compile(rf"http://(?P<container>[^:/]+):{INSTANCE_PORT}")
# noinspection HttpUrlsUsage
WEB_ADDRESS_URL = f"http://127.0.0.1:{INSTANCE_PORT}/"
CHECK_REQUEST_SECONDS = 2
POLL_SECONDS = 0.5


class ProxyError(Exception):
    pass


class CheckFailed(Exception):
    pass


class HelperHost(Protocol):
    def ensure_helper(self, helper: HelperContainer) -> None: ...
    def is_running(self, container: str) -> bool: ...
    def run_in(self, container: str, command: list[str]) -> CommandResult: ...
    def read_folder(self, container: str, folder: PurePosixPath) -> dict[str, FileRead]: ...
    def read_log_tail(self, container: str) -> str: ...


class Read(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class LoadedRouter(Read):
    rule: str
    service: str
    status: RouteStatus


class LoadedServer(Read):
    url: str


class LoadedBalancer(Read):
    servers: list[LoadedServer] = Field(min_length=1, max_length=1)


class TraefikService(Read):
    load_balancer: LoadedBalancer = Field(alias="loadBalancer")


class LoadedRoutes(Read):
    routers: dict[str, LoadedRouter]
    services: dict[str, TraefikService | dict[str, object]]


ROUTE_FILE_SUFFIX = ".yml"


def route_file_of(web_address: str) -> PurePosixPath:
    return ROUTES_FOLDER / f"{web_address}{ROUTE_FILE_SUFFIX}"


# noinspection HttpUrlsUsage
def instance_url(container: str) -> str:
    return f"http://{container}:{INSTANCE_PORT}"


# A first line, a comment to the proxy, says which installation a route is: one put back is not the file read before.
def route_text(web_address: str, container: str, generation: str = "", attempt: int = 0) -> str:
    router = {"rule": f"Host(`{web_address}`)", "entryPoints": [ENTRY_POINT], "service": web_address}
    target = {"loadBalancer": {"servers": [{"url": instance_url(container)}]}}
    route = json.dumps({"http": {"routers": {web_address: router}, "services": {web_address: target}}}, indent=2)
    return f"# {generation} {attempt}\n{route}" if generation else route


def container_behind(router: LoadedRouter, services: Mapping[str, object]) -> str:
    target = services.get(router.service + FROM_ROUTES_FILE)
    if not isinstance(target, TraefikService):
        raise ProxyError(f"The proxy sends {router.rule} to {router.service}, which is not a Traefik service Launchplane wrote.")
    url = target.load_balancer.servers[0].url
    match = INSTANCE_URL.fullmatch(url)
    if match is None:
        raise ProxyError(f"The proxy sends {router.rule} to {url}, which is not an instance Launchplane started.")
    return match["container"]


def web_address_in(router: LoadedRouter) -> str:
    match = HOST_RULE.fullmatch(router.rule)
    if match is None:
        raise ProxyError(f"The proxy has a route with the rule {router.rule}, which Launchplane did not write.")
    return match["web_address"]


type Loaded = LoadedRoute | Conflicting[LoadedRoute]


# Two routers for one web address are a conflict whatever each sends it to, and in whatever order the proxy lists them.
def lane_routes_by_web_address(routes: LoadedRoutes) -> dict[str, Loaded]:
    seen: dict[str, list[LoadedRoute]] = {}
    for key, router in sorted(routes.routers.items()):
        if key.endswith(FROM_ROUTES_FILE):
            found = seen.setdefault(web_address_in(router), [])
            found.append(LoadedRoute(container_behind(router, routes.services), router.status, key))
    return {
        web_address: found[0] if len(found) == 1 else Conflicting(f"Where the proxy sends {web_address}", tuple(found))
        for web_address, found in seen.items()
    }


# A first line the proxy reads as a comment is not part of the route. A file that is otherwise not the route Launchplane
# writes for its web address is said to be that, for its lane alone.
def written_route_from_file(text: str, web_address: str, written_at: datetime) -> WrittenRoute | Unread:
    first, _, rest = text.partition("\n")
    body = rest if first.startswith("#") else text
    found = INSTANCE_URL.search(body)
    if found is None or body != route_text(web_address, found["container"]):
        return Unread(asked=f"The routes file {route_file_of(web_address)}", why="it is not the route Launchplane writes")
    return WrittenRoute(found["container"], written_at, text, first.removeprefix("# ") if body is rest else "")


def is_loaded(route: Loaded | None, container: str | None, *, enabled: bool) -> bool:
    if not isinstance(route, LoadedRoute) or container is None:
        return route is None and container is None
    return route.container == container and (route.status == RouteStatus.ENABLED or not enabled)


@dataclass(frozen=True)
class TraefikProxy:
    host: HelperHost
    steady_seconds: float = 3
    check_deadline_seconds: float = 20
    load_deadline_seconds: float = 10
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic
    # Every lane's routes file is in the one folder. It is held while the folder is read whole or a file in it is
    # changed, and no longer, so no reading finds a file half put in place by another lane's worker.
    routes_folder: threading.Lock = field(default_factory=threading.Lock, init=False, compare=False, repr=False)

    def is_running(self) -> bool:
        return self.host.is_running(PROXY_NAME)

    def start(self) -> None:
        self.host.ensure_helper(PROXY)
        started = self.clock()
        while not (result := self.host.run_in(PROXY_NAME, ["wget", "-q", "-O", "/dev/null", LOADED_ROUTES_URL])).succeeded:
            if self.clock() - started >= self.load_deadline_seconds:
                raise ProxyError(f"The proxy did not answer within {self.load_deadline_seconds:g} s of starting: {result.output}")
            self.sleep(POLL_SECONDS)

    @observing()
    def read_routes(self) -> dict[str, Loaded]:
        result = self.host.run_in(PROXY_NAME, ["wget", "-q", "-O", "-", LOADED_ROUTES_URL])
        if not result.succeeded:
            raise ProxyError(f"Reading the proxy's loaded routes failed: {result.output}")
        try:
            return lane_routes_by_web_address(LoadedRoutes.model_validate_json(result.output))
        except ValidationError as error:
            raise ProxyError(
                f"The proxy's loaded routes did not have the expected shape, so nothing was changed: {error}"
            ) from error

    def read_folder(self) -> dict[str, FileRead]:
        with self.routes_folder:
            return self.host.read_folder(PROXY_NAME, ROUTES_FOLDER)

    # The route files are read from the proxy's container even while it is stopped; what it has loaded only while it runs.
    # A file that is not a route Launchplane writes is its own lane's fault, and no other lane's reading fails over it.
    @observing()
    def read(self) -> ProxySeen:
        runs = self.is_running()
        written = {
            name.removesuffix(ROUTE_FILE_SUFFIX): written_route_from_file(
                file.text, name.removesuffix(ROUTE_FILE_SUFFIX), file.changed_at
            )
            for name, file in self.read_folder().items()
            if name.endswith(ROUTE_FILE_SUFFIX)
        }
        return ProxySeen(runs, self.host.is_running(BACK_SOON), written, self.read_routes() if runs else {})

    def await_route(self, web_address: str, container: str | None, *, enabled: bool = False) -> None:
        started = self.clock()
        while not is_loaded(loaded := self.read_routes().get(web_address), container, enabled=enabled):
            if self.clock() - started >= self.load_deadline_seconds:
                raise ProxyError(
                    f"The proxy did not load the routes file of {web_address} within {self.load_deadline_seconds:g} s: "
                    f"it is written to send {web_address} to {container}, and the proxy has loaded: {loaded or 'no route'}. "
                    f"The proxy's last log lines:\n{self.host.read_log_tail(PROXY_NAME)}"
                )
            self.sleep(POLL_SECONDS)

    @property
    def gate(self) -> Gate:
        return Gate(self.host, PROXY_NAME, VOLUME_FOLDER, self.routes_folder)

    # The one moment a release becomes real, asked of the gate for the exact candidate of one attempt.
    def switch(self, web_address: str, generation: str, attempt: int, candidate: str) -> Answer:
        text = route_text(web_address, candidate, generation, attempt)
        return self.gate.switch(web_address, generation, attempt, candidate, text, route_file_of(web_address))

    def edit(self, web_address: str, generation: str, attempt: int, read: str, container: str | None) -> str:
        if container == BACK_SOON:
            self.host.ensure_helper(BACK_SOON_PAGE)
        text = "" if container is None else route_text(web_address, container, generation, attempt)
        return self.gate.edit(web_address, generation, attempt, read, text, route_file_of(web_address))

    def fetch(self, *request: str) -> CommandResult:
        return self.host.run_in(PROXY_NAME, ["wget", "-q", "-O", "/dev/null", "-T", str(CHECK_REQUEST_SECONDS), *request])

    def request_instance(self, container: str, health_path: str) -> CommandResult:
        return self.fetch(instance_url(container) + health_path)

    def why_web_address_fails(self, web_address: str) -> str | None:
        result = self.fetch("--header", f"Host: {web_address}", WEB_ADDRESS_URL)
        return None if result.succeeded else result.output

    def check_instance(self, container: str, health_path: str, command: Sequence[str] = ()) -> None:
        started = self.clock()
        answering_since: float | None = None
        while True:
            result = self.request_instance(container, health_path)
            now = self.clock()
            if not result.succeeded:
                answering_since = None
                if now - started >= self.check_deadline_seconds:
                    raise CheckFailed(
                        f"{instance_url(container)}{health_path} did not answer the proxy with success "
                        f"within {self.check_deadline_seconds:g} s: {result.output}"
                    )
            elif answering_since is None:
                answering_since = now
            elif now - answering_since >= self.steady_seconds:
                break
            self.sleep(POLL_SECONDS)
        if command and not (ran := self.host.run_in(container, [*command])).succeeded:
            raise CheckFailed(f"{container} answers the proxy, and the check of its kind, run in it, failed: {ran.output}")
