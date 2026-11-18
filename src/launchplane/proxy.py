import json
import re
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from importlib.resources import files
from pathlib import PurePosixPath
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from launchplane.container_host import FileRead, HelperContainer
from launchplane.docker_answers import CommandResult
from launchplane.instance import BACK_SOON, LANE_NETWORK, LoadedRoute, ProxySeen, RouteStatus, WrittenRoute, is_generation

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
WRITTEN_BY_PREFIX = "# launchplane.generation="
WRITTEN_BY = re.compile(rf"{re.escape(WRITTEN_BY_PREFIX)}(?P<generation>[0-9a-f-]{{36}})\n")
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


# A routes file waiting to be renamed into place: a release's is named for its candidate, so removing it refuses a
# rename already sent for that candidate and for no other.
def staged_route_file_of(web_address: str, container: str | None = None) -> PurePosixPath:
    return ROUTES_FOLDER / (f"{web_address}.next" if container is None else f"{web_address}.{container}.next")


ABANDONED_SUFFIX, COMMITTED_SUFFIX = ".abandoned", ".committed"


def abandoned_mark_of(web_address: str, generation: str) -> PurePosixPath:
    return ROUTES_FOLDER / f"{web_address}.{generation}{ABANDONED_SUFFIX}"


def committed_mark_of(web_address: str, generation: str) -> PurePosixPath:
    return ROUTES_FOLDER / f"{web_address}.{generation}{COMMITTED_SUFFIX}"


# The gate. Everything that settles a release runs as one command on the container host, holding the one lock file
# there, so each finds what any earlier one did, whichever Launchplane sent it and however late it arrives. A verdict is
# a file of the generation's own, made once and replaced by nothing done for another generation. Each answers LOST when
# what it finds has settled the matter against it.
GATE = ROUTES_FOLDER / ".gate"
LOST = 3
# Renames for a candidate, unless its generation is abandoned or the candidate's staged file was taken away.
SWITCH = f'if [ -e "$1" ] || [ ! -e "$2" ]; then rm -f "$2"; exit {LOST}; fi; mv "$2" "$3" && : > "$4"'
# Marks a generation abandoned, unless the routes file was renamed for it, now or ever.
ABANDON = f'if [ -e "$1" ] || head -n 1 "$2" 2>/dev/null | grep -qxF "$3"; then exit {LOST}; fi; : > "$4"'
# Takes a candidate's staged file away, unless the routes file sends visitors to the candidate.
DISCARD = f'if grep -qF "$2" "$1" 2>/dev/null; then exit {LOST}; fi; rm -f "$3"'
REMOVE = f'rm -f "$1" "$2".*{ABANDONED_SUFFIX} "$2".*{COMMITTED_SUFFIX} "$2".*.next'


# noinspection HttpUrlsUsage
def instance_url(container: str) -> str:
    return f"http://{container}:{INSTANCE_PORT}"


def stamp_line(generation: str) -> str:
    return f"{WRITTEN_BY_PREFIX}{generation}"


# The routes file is what sends visitors somewhere, so it says which generation it was renamed into place for: on a first
# line the proxy reads as a comment.
def route_text(web_address: str, container: str, generation: str | None = None) -> str:
    router = {"rule": f"Host(`{web_address}`)", "entryPoints": [ENTRY_POINT], "service": web_address}
    target = {"loadBalancer": {"servers": [{"url": instance_url(container)}]}}
    written_by = "" if generation is None else f"{stamp_line(generation)}\n"
    return written_by + json.dumps({"http": {"routers": {web_address: router}, "services": {web_address: target}}}, indent=2)


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


STAMP_PREFIX = "# launchplane."


# A first line that is a stamp of any kind is a comment to the proxy. One that names a generation says which release the
# file was renamed for. An older stamp, or none, is a route no release of this code renamed, and is a route all the same.
def written_route_from_file(text: str, web_address: str, written_at: datetime) -> WrittenRoute | str:
    stamp = WRITTEN_BY.match(text)
    first, _, rest = text.partition("\n")
    body = rest if first.startswith(STAMP_PREFIX) else text
    found = INSTANCE_URL.search(body)
    if found is None or body != route_text(web_address, found["container"]):
        return f"The routes file {route_file_of(web_address)} is not the route Launchplane writes for {web_address}"
    return WrittenRoute(found["container"], written_at, None if stamp is None else stamp["generation"])


def abandoned_from(file_names: Iterable[str]) -> dict[str, frozenset[str]]:
    marks = (name.removesuffix(ABANDONED_SUFFIX).rpartition(".") for name in file_names if name.endswith(ABANDONED_SUFFIX))
    abandoned: dict[str, frozenset[str]] = {}
    for web_address, _, generation in marks:
        if is_generation(generation):
            abandoned[web_address] = abandoned.get(web_address, frozenset()) | {generation}
    return abandoned


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

    def read_folder(self) -> dict[str, FileRead]:
        with self.routes_folder:
            return self.host.read_folder(PROXY_NAME, ROUTES_FOLDER)

    # The route files are read from the proxy's container even while it is stopped; what it has loaded only while it runs.
    # A file that is not a route Launchplane writes is its own lane's fault, and no other lane's reading fails over it.
    def read(self) -> ProxySeen:
        runs = self.host.is_running(PROXY_NAME)
        files_read = self.read_folder()
        routes = {
            name.removesuffix(ROUTE_FILE_SUFFIX): written_route_from_file(
                file.text, name.removesuffix(ROUTE_FILE_SUFFIX), file.changed_at
            )
            for name, file in files_read.items()
            if name.endswith(ROUTE_FILE_SUFFIX)
        }
        return ProxySeen(
            runs=runs,
            back_soon_runs=self.host.is_running(BACK_SOON),
            written={web_address: route for web_address, route in routes.items() if isinstance(route, WrittenRoute)},
            loaded=self.read_loaded() if runs else {},
            abandoned=abandoned_from(files_read),
            unread={web_address: why for web_address, why in routes.items() if isinstance(why, str)},
        )

    def show_back_soon(self, web_address: str) -> None:
        self.host.ensure_helper(BACK_SOON_PAGE)
        self.write_route(web_address, BACK_SOON)

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
        route = str(route_file_of(web_address))
        with self.routes_folder:
            self.run_or_fail(
                ["sh", "-c", REMOVE, "sh", route, route.removesuffix(ROUTE_FILE_SUFFIX)], f"Removing the route of {web_address}"
            )

    def loaded_container(self, web_address: str) -> str | None:
        route = self.read_loaded().get(web_address)
        return None if route is None else route.container

    def write_route(self, web_address: str, container: str) -> None:
        with self.routes_folder:
            self.host.place_file(PROXY_NAME, staged_route_file_of(web_address), route_text(web_address, container))
            self.run_or_fail(
                ["mv", str(staged_route_file_of(web_address)), str(route_file_of(web_address))],
                f"Putting the route of {web_address} in place",
            )

    # Whether the command won at the gate. The lock is the host's, so the command is decided there and not here.
    def pass_gate(self, script: str, arguments: Iterable[PurePosixPath | str], attempt: str) -> bool:
        with self.routes_folder:
            result = self.host.run_in(PROXY_NAME, ["flock", str(GATE), "sh", "-c", script, "sh", *map(str, arguments)])
        if result.exit_code not in (0, LOST):
            raise ProxyError(f"{attempt} failed: {result.output}")
        return result.succeeded

    # The rename is the one moment a release becomes real. It is not made once a cancel has won against the generation,
    # or once the candidate was discarded.
    def switch(self, web_address: str, container: str, generation: str) -> bool:
        staged = staged_route_file_of(web_address, container)
        self.host.place_file(PROXY_NAME, staged, route_text(web_address, container, generation))
        marks = abandoned_mark_of(web_address, generation), committed_mark_of(web_address, generation)
        arguments = [marks[0], staged, route_file_of(web_address), marks[1]]
        return self.pass_gate(SWITCH, arguments, f"Putting the route of {web_address} in place")

    # A cancel wins unless the routes file was renamed for the generation. Winning again changes nothing.
    def abandon(self, web_address: str, generation: str) -> bool:
        marks = committed_mark_of(web_address, generation), abandoned_mark_of(web_address, generation)
        arguments = [marks[0], route_file_of(web_address), stamp_line(generation), marks[1]]
        return self.pass_gate(ABANDON, arguments, f"Marking the release of {web_address} as cancelled")

    # Before an instance that receives no visitors is removed: a rename still on its way to it is refused from here on,
    # or has already happened, and then the instance is the one the routes file names and is not removed.
    def discard(self, web_address: str, container: str) -> bool:
        arguments = [route_file_of(web_address), f'"{instance_url(container)}"', staged_route_file_of(web_address, container)]
        return self.pass_gate(DISCARD, arguments, f"Refusing a switch of {web_address} to {container}")

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
