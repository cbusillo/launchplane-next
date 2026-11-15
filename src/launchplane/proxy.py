import json
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from launchplane.container_host import CommandResult, Service
from launchplane.decide import LaneRoute, LoadedRoute, ProxyNotRunning, RouteStatus

PROXY_NAME = "launchplane-proxy"
ROUTES_FOLDER = PurePosixPath("/etc/traefik/launchplane")
ENTRY_POINT = "web"
COPY_PORT = 80
PROXY = Service(
    name=PROXY_NAME,
    image="traefik:v3.7.18",
    command=[
        f"--entrypoints.{ENTRY_POINT}.address=:{COPY_PORT}",
        "--entrypoints.traefik.address=127.0.0.1:8080",
        "--api.insecure=true",
        f"--providers.file.directory={ROUTES_FOLDER}",
        "--providers.file.watch=true",
    ],
    ports={80: 80},
    volumes={"launchplane-proxy-routes": str(ROUTES_FOLDER)},
)
LOADED_ROUTES_URL = "http://127.0.0.1:8080/api/rawdata"
FROM_ROUTES_FILE = "@file"
HOST_RULE = re.compile(r"Host\(`(?P<name>[^`]+)`\)")
COPY_URL = re.compile(rf"http://(?P<copy>[^:/]+):{COPY_PORT}")
# noinspection HttpUrlsUsage
NAME_URL = f"http://127.0.0.1:{COPY_PORT}/"
CHECK_REQUEST_SECONDS = 2
POLL_SECONDS = 0.5


class ProxyError(Exception):
    pass


class CheckFailed(Exception):
    pass


class ServiceHost(Protocol):
    def ensure_service(self, service: Service) -> None: ...
    def is_running(self, name: str) -> bool: ...
    def run_in(self, name: str, command: list[str]) -> CommandResult: ...
    def read_file(self, name: str, path: PurePosixPath) -> str | None: ...
    def place_file(self, name: str, path: PurePosixPath, content: str) -> None: ...
    def read_log_tail(self, name: str) -> str: ...


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


class LoadedService(Read):
    load_balancer: LoadedBalancer = Field(alias="loadBalancer")


class LoadedRoutes(Read):
    routers: dict[str, LoadedRouter]
    services: dict[str, LoadedService | dict[str, object]]


def route_file_of(name: str) -> PurePosixPath:
    return ROUTES_FOLDER / f"{name}.yml"


def staged_route_file_of(name: str) -> PurePosixPath:
    return ROUTES_FOLDER / f"{name}.next"


# noinspection HttpUrlsUsage
def copy_url(copy_name: str) -> str:
    return f"http://{copy_name}:{COPY_PORT}"


def route_text(name: str, copy_name: str) -> str:
    router = {"rule": f"Host(`{name}`)", "entryPoints": [ENTRY_POINT], "service": name}
    service = {"loadBalancer": {"servers": [{"url": copy_url(copy_name)}]}}
    return json.dumps({"http": {"routers": {name: router}, "services": {name: service}}}, indent=2)


def copy_behind(router: LoadedRouter, services: Mapping[str, object]) -> str:
    service = services.get(router.service + FROM_ROUTES_FILE)
    if not isinstance(service, LoadedService):
        raise ProxyError(f"The proxy sends {router.rule} to {router.service}, which is not a service Launchplane wrote.")
    url = service.load_balancer.servers[0].url
    match = COPY_URL.fullmatch(url)
    if match is None:
        raise ProxyError(f"The proxy sends {router.rule} to {url}, which is not a copy Launchplane started.")
    return match["copy"]


def name_of(router: LoadedRouter) -> str:
    match = HOST_RULE.fullmatch(router.rule)
    if match is None:
        raise ProxyError(f"The proxy has a route with the rule {router.rule}, which Launchplane did not write.")
    return match["name"]


def lane_routes_by_name(routes: LoadedRoutes) -> dict[str, LoadedRoute]:
    from_files = (router for key, router in routes.routers.items() if key.endswith(FROM_ROUTES_FILE))
    return {name_of(router): LoadedRoute(copy_behind(router, routes.services), router.status) for router in from_files}


def loaded_routes_from_answer(answer: str) -> dict[str, LoadedRoute]:
    try:
        routes = LoadedRoutes.model_validate_json(answer)
    except ValidationError as error:
        raise ProxyError(f"The proxy's loaded routes did not have the expected shape, so nothing was changed: {error}") from error
    return lane_routes_by_name(routes)


def written_route_from_file(text: str, name: str) -> str:
    found = COPY_URL.search(text)
    if found is None or text != route_text(name, found["copy"]):
        raise ProxyError(f"The routes file {route_file_of(name)} is not the route Launchplane writes for {name}.")
    return found["copy"]


@dataclass(frozen=True)
class TraefikProxy:
    host: ServiceHost
    steady_seconds: float = 3
    check_deadline_seconds: float = 20
    load_deadline_seconds: float = 10
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic

    def start(self) -> None:
        self.host.ensure_service(PROXY)
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

    def read_serving(self) -> dict[str, str]:
        if not self.host.is_running(PROXY_NAME):
            return {}
        return {name: route.copy for name, route in self.read_loaded().items() if route.status == RouteStatus.ENABLED}

    def read_lane_route(self, name: str) -> ProxyNotRunning | LaneRoute:
        if not self.host.is_running(PROXY_NAME):
            return ProxyNotRunning()
        text = self.host.read_file(PROXY_NAME, route_file_of(name))
        written = None if text is None else written_route_from_file(text, name)
        return LaneRoute(loaded=self.read_loaded().get(name), written=written)

    def await_route(self, name: str, copy_name: str | None) -> None:
        started = self.clock()
        while (loaded := self.loaded_copy(name)) != copy_name:
            if self.clock() - started >= self.load_deadline_seconds:
                raise ProxyError(
                    f"The proxy did not load the routes file of {name} within {self.load_deadline_seconds:g} s: "
                    f"it is written to send {name} to {copy_name} and the proxy still sends it to {loaded}. "
                    f"The proxy's last log lines:\n{self.host.read_log_tail(PROXY_NAME)}"
                )
            self.sleep(POLL_SECONDS)

    def remove_route(self, name: str) -> None:
        self.run_or_fail(["rm", "-f", str(route_file_of(name))], f"Removing the route of {name}")

    def loaded_copy(self, name: str) -> str | None:
        route = self.read_loaded().get(name)
        return None if route is None else route.copy

    def write_route(self, name: str, copy_name: str) -> None:
        self.host.place_file(PROXY_NAME, staged_route_file_of(name), route_text(name, copy_name))
        self.run_or_fail(["mv", str(staged_route_file_of(name)), str(route_file_of(name))], f"Putting the route of {name} in place")

    def run_or_fail(self, command: list[str], attempt: str) -> None:
        result = self.host.run_in(PROXY_NAME, command)
        if not result.succeeded:
            raise ProxyError(f"{attempt} failed: {result.output}")

    def request_copy(self, copy_name: str) -> CommandResult:
        url = copy_url(copy_name) + "/"
        return self.host.run_in(PROXY_NAME, ["wget", "-q", "-O", "/dev/null", "-T", str(CHECK_REQUEST_SECONDS), url])

    def why_name_fails(self, name: str) -> str | None:
        command = ["wget", "-q", "-O", "/dev/null", "-T", str(CHECK_REQUEST_SECONDS), "--header", f"Host: {name}", NAME_URL]
        result = self.host.run_in(PROXY_NAME, command)
        return None if result.succeeded else result.output

    def check_copy(self, copy_name: str) -> None:
        started = self.clock()
        answering_since: float | None = None
        while True:
            result = self.request_copy(copy_name)
            now = self.clock()
            if not result.succeeded:
                answering_since = None
                if now - started >= self.check_deadline_seconds:
                    raise CheckFailed(
                        f"{copy_url(copy_name)}/ did not answer the proxy with success within {self.check_deadline_seconds:g} s: "
                        f"{result.output}"
                    )
            elif answering_since is None:
                answering_since = now
            elif now - answering_since >= self.steady_seconds:
                return
            self.sleep(POLL_SECONDS)
