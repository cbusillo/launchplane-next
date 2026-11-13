import time
from collections.abc import Callable
from pathlib import PurePosixPath
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from launchplane.container_host import CommandResult, Service

PROXY_NAME = "launchplane-proxy"
PROXY = Service(
    name=PROXY_NAME,
    image="caddy:2.11.9-alpine",
    command=["caddy", "run", "--resume"],
    ports={80: 80},
    volumes={"launchplane-proxy-config": "/config", "launchplane-proxy-data": "/data"},
)
ADMIN_CONFIG_URL = "http://127.0.0.1:2019/config/"
CONFIG_PATH = PurePosixPath("/config/launchplane-next.json")
SERVER_NAME = "lanes"
COPY_PORT = 80
CHECK_REQUEST_SECONDS = 2


class NameSwitchError(Exception):
    pass


class CheckFailed(Exception):
    pass


class ServiceHost(Protocol):
    def ensure_service(self, service: Service) -> None: ...
    def is_running(self, name: str) -> bool: ...
    def run_in(self, name: str, command: list[str]) -> CommandResult: ...
    def place_file(self, name: str, path: PurePosixPath, content: str) -> None: ...


class Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class Upstream(Closed):
    dial: str


class ReverseProxy(Closed):
    handler: Literal["reverse_proxy"] = "reverse_proxy"
    upstreams: list[Upstream] = Field(min_length=1, max_length=1)


class HostMatch(Closed):
    host: list[str] = Field(min_length=1, max_length=1)


class Route(Closed):
    id: str = Field(alias="@id")
    match: list[HostMatch] = Field(min_length=1, max_length=1)
    handle: list[ReverseProxy] = Field(min_length=1, max_length=1)


class Server(Closed):
    listen: list[str]
    routes: list[Route] = []


class Http(Closed):
    servers: dict[Literal["lanes"], Server]


class Apps(Closed):
    http: Http


class ProxyConfig(Closed):
    apps: Apps


PROXY_CONFIG = TypeAdapter(ProxyConfig | None)


def route_for(name: str, copy_name: str) -> Route:
    return Route.model_validate(
        {
            "@id": name,
            "match": [{"host": [name]}],
            "handle": [{"upstreams": [{"dial": f"{copy_name}:{COPY_PORT}"}]}],
        }
    )


def copies_by_name(config: ProxyConfig | None) -> dict[str, str]:
    if config is None:
        return {}
    routes = config.apps.http.servers[SERVER_NAME].routes
    return {route.id: route.handle[0].upstreams[0].dial.rsplit(":", 1)[0] for route in routes}


def config_pointing(copies: dict[str, str]) -> ProxyConfig:
    routes = [route_for(name, copy_name) for name, copy_name in sorted(copies.items())]
    return ProxyConfig(apps=Apps(http=Http(servers={SERVER_NAME: Server(listen=[f":{COPY_PORT}"], routes=routes)})))


def copy_url(copy_name: str) -> str:
    return f"http://{copy_name}:{COPY_PORT}/"


def config_from_answer(answer: str) -> ProxyConfig | None:
    try:
        return PROXY_CONFIG.validate_json(answer)
    except ValidationError as error:
        raise NameSwitchError(f"The proxy's configuration is not one Launchplane wrote, so nothing was changed: {error}") from error


class NameSwitch:
    def __init__(
        self,
        host: ServiceHost,
        steady_seconds: float = 3,
        check_deadline_seconds: float = 20,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.host = host
        self.steady_seconds = steady_seconds
        self.check_deadline_seconds = check_deadline_seconds
        self.sleep = sleep
        self.clock = clock

    def ensure_ready(self) -> None:
        self.host.ensure_service(PROXY)

    def read_config(self) -> ProxyConfig | None:
        result = self.host.run_in(PROXY_NAME, ["wget", "-q", "-O", "-", ADMIN_CONFIG_URL])
        if not result.succeeded:
            raise NameSwitchError(f"Reading the proxy's configuration failed: {result.output}")
        return config_from_answer(result.output)

    def read_serving(self) -> dict[str, str]:
        if not self.host.is_running(PROXY_NAME):
            return {}
        return copies_by_name(self.read_config())

    def point(self, name: str, copy_name: str) -> None:
        copies = copies_by_name(self.read_config()) | {name: copy_name}
        self.host.place_file(PROXY_NAME, CONFIG_PATH, config_pointing(copies).model_dump_json(by_alias=True))
        result = self.host.run_in(PROXY_NAME, ["caddy", "reload", "--config", str(CONFIG_PATH)])
        if not result.succeeded:
            raise NameSwitchError(f"The proxy refused to point {name} at {copy_name}: {result.output}")

    def request_copy(self, copy_name: str) -> CommandResult:
        url = copy_url(copy_name)
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
                        f"{copy_url(copy_name)} did not answer the proxy with success within {self.check_deadline_seconds:g} s: "
                        f"{result.output}"
                    )
            elif answering_since is None:
                answering_since = now
            elif now - answering_since >= self.steady_seconds:
                return
            self.sleep(0.5)
