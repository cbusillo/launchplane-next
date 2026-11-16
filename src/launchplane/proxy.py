import json
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from launchplane.container_host import CommandResult, HelperContainer
from launchplane.instance import LANE_NETWORK, LaneRoute, LoadedRoute, ProxyNotRunning, RouteStatus

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
    networks=["launchplane-proxy-outside", LANE_NETWORK],
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
    def read_file(self, container: str, path: PurePosixPath) -> str | None: ...
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


def route_file_of(web_address: str) -> PurePosixPath:
    return ROUTES_FOLDER / f"{web_address}.yml"


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


def loaded_routes_from_answer(answer: str) -> dict[str, LoadedRoute]:
    try:
        routes = LoadedRoutes.model_validate_json(answer)
    except ValidationError as error:
        raise ProxyError(f"The proxy's loaded routes did not have the expected shape, so nothing was changed: {error}") from error
    return lane_routes_by_web_address(routes)


def written_route_from_file(text: str, web_address: str) -> str:
    found = INSTANCE_URL.search(text)
    if found is None or text != route_text(web_address, found["container"]):
        raise ProxyError(f"The routes file {route_file_of(web_address)} is not the route Launchplane writes for {web_address}.")
    return found["container"]


@dataclass(frozen=True)
class TraefikProxy:
    host: HelperHost
    steady_seconds: float = 3
    check_deadline_seconds: float = 20
    load_deadline_seconds: float = 10
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic

    def start(self) -> None:
        self.host.ensure_helper(PROXY)
        started = self.clock()
        while not (result := self.host.run_in(PROXY_NAME, ["wget", "-q", "-O", "/dev/null", LOADED_ROUTES_URL])).succeeded:
            if self.clock() - started >= self.load_deadline_seconds:
                raise ProxyError(f"The proxy did not answer within {self.load_deadline_seconds:g} s of starting: {result.output}")
            self.sleep(POLL_SECONDS)

    def read_loaded(self) -> dict[str, LoadedRoute]:
        result = self.host.run_in(PROXY_NAME, ["wget", "-q", "-O", "-", LOADED_ROUTES_URL])
        if not result.succeeded:
            raise ProxyError(f"Reading the proxy's loaded routes failed: {result.output}")
        return loaded_routes_from_answer(result.output)

    def read_enabled_routes(self) -> dict[str, str]:
        if not self.host.is_running(PROXY_NAME):
            return {}
        return {
            web_address: route.container for web_address, route in self.read_loaded().items() if route.status == RouteStatus.ENABLED
        }

    def read_lane_route(self, web_address: str) -> ProxyNotRunning | LaneRoute:
        if not self.host.is_running(PROXY_NAME):
            return ProxyNotRunning()
        text = self.host.read_file(PROXY_NAME, route_file_of(web_address))
        written = None if text is None else written_route_from_file(text, web_address)
        return LaneRoute(loaded=self.read_loaded().get(web_address), written=written)

    def await_route(self, web_address: str, container: str | None) -> None:
        started = self.clock()
        while (loaded := self.loaded_container(web_address)) != container:
            if self.clock() - started >= self.load_deadline_seconds:
                raise ProxyError(
                    f"The proxy did not load the routes file of {web_address} within {self.load_deadline_seconds:g} s: "
                    f"it is written to send {web_address} to {container} and the proxy still sends it to {loaded}. "
                    f"The proxy's last log lines:\n{self.host.read_log_tail(PROXY_NAME)}"
                )
            self.sleep(POLL_SECONDS)

    def remove_route(self, web_address: str) -> None:
        self.run_or_fail(["rm", "-f", str(route_file_of(web_address))], f"Removing the route of {web_address}")

    def loaded_container(self, web_address: str) -> str | None:
        route = self.read_loaded().get(web_address)
        return None if route is None else route.container

    def write_route(self, web_address: str, container: str) -> None:
        self.host.place_file(PROXY_NAME, staged_route_file_of(web_address), route_text(web_address, container))
        self.run_or_fail(
            ["mv", str(staged_route_file_of(web_address)), str(route_file_of(web_address))],
            f"Putting the route of {web_address} in place",
        )

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
