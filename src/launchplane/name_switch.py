import json
import re
import time
from collections.abc import Callable
from pathlib import PurePosixPath
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from launchplane.container_host import CommandResult, Service

PROXY_NAME = "launchplane-proxy"
ROUTES_FOLDER = PurePosixPath("/etc/traefik/launchplane")
ROUTES_FILE = ROUTES_FOLDER / "lanes.yml"
STAGED_ROUTES_FILE = ROUTES_FOLDER / "lanes.next"
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
CHECK_REQUEST_SECONDS = 2
POLL_SECONDS = 0.5


class NameSwitchError(Exception):
    pass


class CheckFailed(Exception):
    pass


class ServiceHost(Protocol):
    def ensure_service(self, service: Service) -> None: ...
    def is_running(self, name: str) -> bool: ...
    def run_in(self, name: str, command: list[str]) -> CommandResult: ...
    def place_file(self, name: str, path: PurePosixPath, content: str) -> None: ...
    def read_log_tail(self, name: str) -> str: ...


class Read(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class LoadedRouter(Read):
    rule: str
    service: str


class LoadedServer(Read):
    url: str


class LoadedBalancer(Read):
    servers: list[LoadedServer] = Field(min_length=1, max_length=1)


class LoadedService(Read):
    load_balancer: LoadedBalancer = Field(alias="loadBalancer")


class LoadedRoutes(Read):
    routers: dict[str, LoadedRouter] = {}
    services: dict[str, LoadedService | dict[str, object]] = {}


def is_from_routes_file(key: str) -> bool:
    return key.endswith(FROM_ROUTES_FILE)


def copy_behind(router: LoadedRouter, routes: LoadedRoutes) -> str:
    service = routes.services.get(router.service + FROM_ROUTES_FILE)
    if not isinstance(service, LoadedService):
        raise NameSwitchError(f"The proxy sends {router.rule} to {router.service}, which is not a service Launchplane wrote.")
    url = service.load_balancer.servers[0].url
    match = COPY_URL.fullmatch(url)
    if match is None:
        raise NameSwitchError(f"The proxy sends {router.rule} to {url}, which is not a copy Launchplane started.")
    return match["copy"]


def name_of(router: LoadedRouter) -> str:
    match = HOST_RULE.fullmatch(router.rule)
    if match is None:
        raise NameSwitchError(f"The proxy has a route with the rule {router.rule}, which Launchplane did not write.")
    return match["name"]


def copies_by_name(routes: LoadedRoutes) -> dict[str, str]:
    file_routers = (router for key, router in routes.routers.items() if is_from_routes_file(key))
    return {name_of(router): copy_behind(router, routes) for router in file_routers}


def loaded_routes_from_answer(answer: str) -> LoadedRoutes:
    try:
        return LoadedRoutes.model_validate_json(answer)
    except ValidationError as error:
        raise NameSwitchError(
            f"The proxy's loaded routes did not have the expected shape, so nothing was changed: {error}"
        ) from error


# noinspection HttpUrlsUsage
def copy_url(copy_name: str) -> str:
    return f"http://{copy_name}:{COPY_PORT}"


def routes_file(copies: dict[str, str]) -> str:
    routers = {name: {"rule": f"Host(`{name}`)", "entryPoints": [ENTRY_POINT], "service": name} for name in copies}
    services = {name: {"loadBalancer": {"servers": [{"url": copy_url(copy)}]}} for name, copy in copies.items()}
    return json.dumps({"http": {"routers": routers, "services": services}}, indent=2)


class NameSwitch:
    def __init__(
        self,
        host: ServiceHost,
        steady_seconds: float = 3,
        check_deadline_seconds: float = 20,
        load_deadline_seconds: float = 10,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.host = host
        self.steady_seconds = steady_seconds
        self.check_deadline_seconds = check_deadline_seconds
        self.load_deadline_seconds = load_deadline_seconds
        self.sleep = sleep
        self.clock = clock

    def ensure_ready(self) -> None:
        self.host.ensure_service(PROXY)
        self.wait_until_answering()

    def wait_until_answering(self) -> None:
        started = self.clock()
        while not (result := self.host.run_in(PROXY_NAME, ["wget", "-q", "-O", "/dev/null", LOADED_ROUTES_URL])).succeeded:
            if self.clock() - started >= self.load_deadline_seconds:
                raise NameSwitchError(f"The proxy did not answer within {self.load_deadline_seconds:g} s: {result.output}")
            self.sleep(POLL_SECONDS)

    def read_loaded_routes(self) -> LoadedRoutes:
        result = self.host.run_in(PROXY_NAME, ["wget", "-q", "-O", "-", LOADED_ROUTES_URL])
        if not result.succeeded:
            raise NameSwitchError(f"Reading the proxy's loaded routes failed: {result.output}")
        return loaded_routes_from_answer(result.output)

    def read_serving(self) -> dict[str, str]:
        if not self.host.is_running(PROXY_NAME):
            return {}
        return copies_by_name(self.read_loaded_routes())

    def write_routes(self, copies: dict[str, str]) -> None:
        self.host.place_file(PROXY_NAME, STAGED_ROUTES_FILE, routes_file(copies))
        result = self.host.run_in(PROXY_NAME, ["mv", str(STAGED_ROUTES_FILE), str(ROUTES_FILE)])
        if not result.succeeded:
            raise NameSwitchError(f"Putting the proxy's new routes in place failed: {result.output}")

    def point(self, name: str, copy_name: str) -> None:
        self.write_routes(self.read_serving() | {name: copy_name})
        started = self.clock()
        while (serving := self.read_serving().get(name)) != copy_name:
            if self.clock() - started >= self.load_deadline_seconds:
                raise NameSwitchError(
                    f"The proxy did not load the route of {name} to {copy_name} within {self.load_deadline_seconds:g} s, "
                    f"and still sends it to {serving}. The proxy's last log lines:\n{self.host.read_log_tail(PROXY_NAME)}"
                )
            self.sleep(POLL_SECONDS)

    def request_copy(self, copy_name: str) -> CommandResult:
        url = copy_url(copy_name) + "/"
        return self.host.run_in(PROXY_NAME, ["wget", "-q", "-O", "/dev/null", "-T", str(CHECK_REQUEST_SECONDS), url])

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
