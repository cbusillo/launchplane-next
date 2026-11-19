import json
import re
import threading
import time
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from importlib.resources import files
from pathlib import PurePosixPath
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from launchplane.container_host import FileRead, HelperContainer
from launchplane.docker_answers import CommandResult, observing
from launchplane.gate import Answer, Gate
from launchplane.instance import BACK_SOON, LANE_NETWORK, LoadedRoute, ProxySeen, RouteStatus, WrittenRoute

PROXY_NAME = "launchplane-proxy"
ROUTES_FOLDER = PurePosixPath("/etc/traefik/launchplane")
ENTRY_POINT = "web"
INSTANCE_PORT = 80
PROXY = HelperContainer(
    container=PROXY_NAME,
    image="traefik:v3.7.18",
    command=[
        f"--entrypoints.{ENTRY_POINT}.address=:{INSTANCE_PORT}",
        "--entrypoints.traefik.address=127.0.0.1:8080",
        "--api.insecure=true",
        f"--providers.file.directory={ROUTES_FOLDER}",
        "--providers.file.watch=true",
    ],
    ports={80: 80},
    volumes={"launchplane-proxy-routes": str(ROUTES_FOLDER)},
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
    ports={},
    volumes={},
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
    def place_file(self, container: str, path: PurePosixPath, content: str) -> None: ...
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


def staged_route_file_of(web_address: str) -> PurePosixPath:
    return ROUTES_FOLDER / f"{web_address}.next"


# noinspection HttpUrlsUsage
def instance_url(container: str) -> str:
    return f"http://{container}:{INSTANCE_PORT}"


def route_text(web_address: str, container: str) -> str:
    router = {"rule": f"Host(`{web_address}`)", "entryPoints": [ENTRY_POINT], "service": web_address}
    target = {"loadBalancer": {"servers": [{"url": instance_url(container)}]}}
    return json.dumps({"http": {"routers": {web_address: router}, "services": {web_address: target}}}, indent=2)


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


def lane_routes_by_web_address(routes: LoadedRoutes) -> dict[str, LoadedRoute]:
    from_files = (router for key, router in routes.routers.items() if key.endswith(FROM_ROUTES_FILE))
    return {web_address_in(router): LoadedRoute(container_behind(router, routes.services), router.status) for router in from_files}


def web_addresses_routed_twice(routes: LoadedRoutes) -> frozenset[str]:
    seen = Counter(web_address_in(router) for key, router in routes.routers.items() if key.endswith(FROM_ROUTES_FILE))
    return frozenset(web_address for web_address, times in seen.items() if times > 1)


def routes_from_answer(answer: str) -> LoadedRoutes:
    try:
        return LoadedRoutes.model_validate_json(answer)
    except ValidationError as error:
        raise ProxyError(f"The proxy's loaded routes did not have the expected shape, so nothing was changed: {error}") from error


def loaded_routes_from_answer(answer: str) -> dict[str, LoadedRoute]:
    return lane_routes_by_web_address(routes_from_answer(answer))


# A first line the proxy reads as a comment is not part of the route. A file that is otherwise not the route Launchplane
# writes for its web address is said to be that, for its lane alone.
def written_route_from_file(text: str, web_address: str, written_at: datetime) -> WrittenRoute | str:
    first, _, rest = text.partition("\n")
    body = rest if first.startswith("#") else text
    found = INSTANCE_URL.search(body)
    if found is None or body != route_text(web_address, found["container"]):
        return f"The routes file {route_file_of(web_address)} is not the route Launchplane writes for {web_address}"
    return WrittenRoute(found["container"], written_at)


def sent_by(route: LoadedRoute | None) -> str:
    if route is None or route.status == RouteStatus.ENABLED:
        return str(None if route is None else route.container)
    return f"{route.container}, with that route {route.status}"


def is_loaded(route: LoadedRoute | None, container: str | None, *, enabled: bool) -> bool:
    if route is None or container is None:
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
    def read_routes(self) -> LoadedRoutes:
        result = self.host.run_in(PROXY_NAME, ["wget", "-q", "-O", "-", LOADED_ROUTES_URL])
        if not result.succeeded:
            raise ProxyError(f"Reading the proxy's loaded routes failed: {result.output}")
        return routes_from_answer(result.output)

    def read_loaded(self) -> dict[str, LoadedRoute]:
        return lane_routes_by_web_address(self.read_routes())

    def read_folder(self) -> dict[str, FileRead]:
        with self.routes_folder:
            return self.host.read_folder(PROXY_NAME, ROUTES_FOLDER)

    # The route files are read from the proxy's container even while it is stopped; what it has loaded only while it runs.
    # A file that is not a route Launchplane writes is its own lane's fault, and no other lane's reading fails over it.
    @observing()
    def read(self) -> ProxySeen:
        runs = self.is_running()
        files_read = self.read_folder()
        routes = {
            name.removesuffix(ROUTE_FILE_SUFFIX): written_route_from_file(
                file.text, name.removesuffix(ROUTE_FILE_SUFFIX), file.changed_at
            )
            for name, file in files_read.items()
            if name.endswith(ROUTE_FILE_SUFFIX)
        }
        loaded = self.read_routes() if runs else LoadedRoutes(routers={}, services={})
        return ProxySeen(
            runs=runs,
            back_soon_runs=self.host.is_running(BACK_SOON),
            written={web_address: route for web_address, route in routes.items() if isinstance(route, WrittenRoute)},
            loaded=lane_routes_by_web_address(loaded),
            unread={web_address: why for web_address, why in routes.items() if isinstance(why, str)},
            routed_twice=web_addresses_routed_twice(loaded),
        )

    def show_back_soon(self, web_address: str) -> None:
        self.host.ensure_helper(BACK_SOON_PAGE)
        self.write_route(web_address, BACK_SOON)

    def await_route(self, web_address: str, container: str | None, *, enabled: bool = False) -> None:
        started = self.clock()
        while not is_loaded(loaded := self.read_loaded().get(web_address), container, enabled=enabled):
            if self.clock() - started >= self.load_deadline_seconds:
                raise ProxyError(
                    f"The proxy did not load the routes file of {web_address} within {self.load_deadline_seconds:g} s: "
                    f"it is written to send {web_address} to {container} and the proxy still sends it to {sent_by(loaded)}. "
                    f"The proxy's last log lines:\n{self.host.read_log_tail(PROXY_NAME)}"
                )
            self.sleep(POLL_SECONDS)

    # The gate's files are not the lane's to remove: a command for one of its releases may still arrive.
    def remove_route(self, web_address: str) -> None:
        with self.routes_folder:
            self.run_or_fail(["rm", "-f", str(route_file_of(web_address))], f"Removing the route of {web_address}")

    def write_route(self, web_address: str, container: str) -> None:
        with self.routes_folder:
            self.host.place_file(PROXY_NAME, staged_route_file_of(web_address), route_text(web_address, container))
            self.run_or_fail(
                ["mv", str(staged_route_file_of(web_address)), str(route_file_of(web_address))],
                f"Putting the route of {web_address} in place",
            )

    @property
    def gate(self) -> Gate:
        return Gate(self.host, PROXY_NAME, ROUTES_FOLDER, self.routes_folder)

    # The one moment a release becomes real, asked of the gate for the exact candidate of one attempt.
    def switch(self, web_address: str, generation: str, attempt: int, candidate: str) -> Answer:
        text = route_text(web_address, candidate)
        return self.gate.switch(web_address, generation, attempt, candidate, text, route_file_of(web_address))

    def run_or_fail(self, command: list[str], attempt: str) -> None:
        result = self.host.run_in(PROXY_NAME, command)
        if not result.succeeded:
            raise ProxyError(f"{attempt} failed: {result.output}")

    def request_instance(self, container: str, health_path: str) -> CommandResult:
        url = instance_url(container) + health_path
        return self.host.run_in(PROXY_NAME, ["wget", "-q", "-O", "/dev/null", "-T", str(CHECK_REQUEST_SECONDS), url])

    def why_web_address_fails(self, web_address: str) -> str | None:
        command = [
            "wget",
            "-q",
            "-O",
            "/dev/null",
            "-T",
            str(CHECK_REQUEST_SECONDS),
            "--header",
            f"Host: {web_address}",
            WEB_ADDRESS_URL,
        ]
        result = self.host.run_in(PROXY_NAME, command)
        return None if result.succeeded else result.output

    def check_instance(self, container: str, health_path: str) -> None:
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
                return
            self.sleep(POLL_SECONDS)
