import json
from pathlib import PurePosixPath

import pytest

from launchplane.container_host import CommandResult, Service
from launchplane.decide import LaneRoute, LoadedRoute, ProxyNotRunning, RouteStatus
from launchplane.proxy import (
    LOADED_ROUTES_URL,
    PROXY_NAME,
    ROUTES_FOLDER,
    CheckFailed,
    ProxyError,
    TraefikProxy,
    route_file_of,
    route_text,
    staged_route_file_of,
)

WEB_ADDRESS = "testing.site.test"
OTHER = "production.site.test"
INTERNAL_ROUTERS = {"api@internal": {"rule": "PathPrefix(`/api`)", "service": "api@internal", "status": "enabled"}}
INTERNAL_SERVICES = {"api@internal": {"status": "enabled"}}


def loaded_from(files: dict[PurePosixPath, str]) -> str:
    routers: dict[str, object] = dict(INTERNAL_ROUTERS)
    services: dict[str, object] = dict(INTERNAL_SERVICES)
    for path, text in files.items():
        if path.suffix == ".yml":
            http = json.loads(text)["http"]
            routers |= {f"{key}@file": router | {"status": "enabled"} for key, router in http["routers"].items()}
            services |= {f"{key}@file": service for key, service in http["services"].items()}
    return json.dumps({"routers": routers, "services": services})


class ProxyHost:
    def __init__(self, routes: dict[str, str], answers: list[bool] | None = None, running: bool = True) -> None:
        self.files = {route_file_of(web_address): route_text(web_address, container) for web_address, container in routes.items()}
        self.loaded = loaded_from(self.files)
        self.answers = iter(answers or [])
        self.running = running
        self.loads_on_write = True
        self.reads_answer = True
        self.log = "error while building configuration"
        self.refused_commands: set[str] = set()

    def load(self) -> None:
        self.loaded = loaded_from(self.files)

    def ensure_service(self, service: Service) -> None:
        self.running = service.container == PROXY_NAME

    def is_running(self, container: str) -> bool:
        return container == PROXY_NAME and self.running

    def run_in(self, container: str, command: list[str]) -> CommandResult:
        assert container == PROXY_NAME
        if command[0] in self.refused_commands:
            return CommandResult(exit_code=1, output=f"{command[0]}: Read-only file system")
        if command[0] == "mv":
            self.files[PurePosixPath(command[2])] = self.files.pop(PurePosixPath(command[1]))
        elif command[0] == "rm":
            self.files.pop(PurePosixPath(command[-1]), None)
        elif command[-1] == LOADED_ROUTES_URL:
            return CommandResult(exit_code=0, output=self.loaded) if self.reads_answer else CommandResult(1, "Connection refused")
        else:
            answered = next(self.answers)
            return CommandResult(exit_code=0 if answered else 1, output="" if answered else "Connection refused")
        if self.loads_on_write:
            self.load()
        return CommandResult(exit_code=0, output="")

    def read_file(self, container: str, path: PurePosixPath) -> str | None:
        assert container == PROXY_NAME
        return self.files.get(path)

    def place_file(self, container: str, path: PurePosixPath, content: str) -> None:
        assert container == PROXY_NAME
        self.files[path] = content

    def read_log_tail(self, container: str) -> str:
        return f"{container}: {self.log}"


def ticking(host: ProxyHost, **seconds: float) -> TraefikProxy:
    clock = iter(range(1000))
    return TraefikProxy(host, sleep=lambda _: host.load(), clock=lambda: next(clock), **seconds)


UNEXPECTED_ANSWERS = {
    "an object with neither routers nor services": {"message": "page not found"},
    "routers without services": {"routers": INTERNAL_ROUTERS},
    "services without routers": {"services": INTERNAL_SERVICES},
    "a router without a rule": {"routers": {"x@file": {"service": "x", "status": "enabled"}}, "services": {}},
    "a router without a status": {"routers": {"x@file": {"rule": "Host(`x`)", "service": "x"}}, "services": {}},
}


@pytest.mark.parametrize("answer", UNEXPECTED_ANSWERS.values(), ids=UNEXPECTED_ANSWERS.keys())
def test_a_proxy_answer_of_unexpected_shape_is_an_error_not_no_routes(answer: object) -> None:
    host = ProxyHost({})
    host.loaded = json.dumps(answer)
    with pytest.raises(ProxyError, match="expected shape"):
        TraefikProxy(host).read_enabled_routes()


def test_a_failed_read_of_the_proxy_is_an_error_not_no_routes() -> None:
    host = ProxyHost({WEB_ADDRESS: "site-t1"})
    host.reads_answer = False
    with pytest.raises(ProxyError, match="Connection refused"):
        TraefikProxy(host).read_lane_route(WEB_ADDRESS)


def test_a_proxy_with_no_routes_of_its_own_sends_no_web_address_anywhere() -> None:
    assert TraefikProxy(ProxyHost({})).read_enabled_routes() == {}


def test_a_proxy_that_is_not_running_is_told_apart_from_one_with_no_routes() -> None:
    assert TraefikProxy(ProxyHost({}, running=False)).read_lane_route(WEB_ADDRESS) == ProxyNotRunning()
    assert TraefikProxy(ProxyHost({})).read_lane_route(WEB_ADDRESS) == LaneRoute(loaded=None, written=None)


def test_a_route_written_and_not_yet_loaded_reads_as_both() -> None:
    host = ProxyHost({WEB_ADDRESS: "site-t1"})
    host.files[route_file_of(WEB_ADDRESS)] = route_text(WEB_ADDRESS, "site-t2")
    assert TraefikProxy(host).read_lane_route(WEB_ADDRESS) == LaneRoute(
        LoadedRoute("site-t1", RouteStatus.ENABLED), written="site-t2"
    )


def test_a_route_launchplane_did_not_write_is_an_error() -> None:
    host = ProxyHost({})
    host.loaded = json.dumps(
        {"routers": {"shop@file": {"rule": "PathPrefix(`/`)", "service": "shop", "status": "enabled"}}, "services": {}}
    )
    with pytest.raises(ProxyError, match="did not write"):
        TraefikProxy(host).read_lane_route(WEB_ADDRESS)


FOREIGN_LANE_FILES = {
    "another lane's route": route_text(OTHER, "site-p1"),
    "its own route plus another": route_text(WEB_ADDRESS, "site-t1").replace(
        f"`{WEB_ADDRESS}`", f"`{WEB_ADDRESS}`) || Host(`{OTHER}`"
    ),
    "its own route to an instance on another port": route_text(WEB_ADDRESS, "site-t1").replace(":80", ":8080"),
}


@pytest.mark.parametrize("text", FOREIGN_LANE_FILES.values(), ids=FOREIGN_LANE_FILES.keys())
def test_a_lane_file_that_is_not_exactly_the_route_launchplane_writes_is_an_error(text: str) -> None:
    host = ProxyHost({})
    host.files[route_file_of(WEB_ADDRESS)] = text
    with pytest.raises(ProxyError, match="not the route Launchplane writes"):
        TraefikProxy(host).read_lane_route(WEB_ADDRESS)


def test_a_route_the_proxy_has_disabled_reads_with_its_status_and_sends_no_visitors() -> None:
    host = ProxyHost({WEB_ADDRESS: "site-t1", OTHER: "site-p1"})
    loaded = json.loads(host.loaded)
    loaded["routers"][f"{WEB_ADDRESS}@file"]["status"] = "disabled"
    host.loaded = json.dumps(loaded)
    assert TraefikProxy(host).read_lane_route(WEB_ADDRESS) == LaneRoute(LoadedRoute("site-t1", RouteStatus.DISABLED), "site-t1")
    assert TraefikProxy(host).read_enabled_routes() == {OTHER: "site-p1"}


def test_writing_a_route_changes_only_that_lanes_file() -> None:
    host = ProxyHost({WEB_ADDRESS: "site-t1", OTHER: "site-p1"})
    other_file = host.files[route_file_of(OTHER)]
    TraefikProxy(host).write_route(WEB_ADDRESS, "site-t2")
    assert host.files == {route_file_of(WEB_ADDRESS): route_text(WEB_ADDRESS, "site-t2"), route_file_of(OTHER): other_file}
    TraefikProxy(host).remove_route(WEB_ADDRESS)
    assert host.files == {route_file_of(OTHER): other_file}


@pytest.mark.parametrize("command", ["mv", "rm"])
def test_a_routes_file_command_that_fails_is_an_error_with_why_not_a_route_taken_as_changed(command: str) -> None:
    host = ProxyHost({WEB_ADDRESS: "site-t1"})
    host.refused_commands.add(command)
    proxy = TraefikProxy(host)
    with pytest.raises(ProxyError, match="Read-only file system"):
        if command == "mv":
            proxy.write_route(WEB_ADDRESS, "site-t2")
        else:
            proxy.remove_route(WEB_ADDRESS)


def test_a_route_is_written_beside_its_file_then_renamed_into_place_so_no_half_file_is_read() -> None:
    assert staged_route_file_of(WEB_ADDRESS).parent == ROUTES_FOLDER
    assert staged_route_file_of(WEB_ADDRESS).suffix not in {".yml", ".yaml", ".toml"}


def test_awaiting_a_route_returns_only_once_the_proxy_has_loaded_it() -> None:
    host = ProxyHost({WEB_ADDRESS: "site-t1"})
    host.loads_on_write = False
    proxy = ticking(host)
    proxy.remove_route(WEB_ADDRESS)
    assert proxy.read_enabled_routes() == {WEB_ADDRESS: "site-t1"}
    proxy.await_route(WEB_ADDRESS, None)
    assert proxy.read_enabled_routes() == {}


def test_a_route_the_proxy_never_loads_is_an_error_carrying_the_proxys_log() -> None:
    host = ProxyHost({WEB_ADDRESS: "site-t1"})
    host.loads_on_write = False
    host.files[route_file_of(WEB_ADDRESS)] = route_text(WEB_ADDRESS, "site-t2")
    proxy = TraefikProxy(host, sleep=lambda _: None, clock=iter(range(1000)).__next__, load_deadline_seconds=3)
    with pytest.raises(
        ProxyError, match=r"send testing.site.test to site-t2 and the proxy still sends it to site-t1[\s\S]*error while"
    ):
        proxy.await_route(WEB_ADDRESS, "site-t2")


def test_an_instance_must_answer_steadily_so_one_answer_between_failures_does_not_pass() -> None:
    host = ProxyHost({}, answers=[True, False, True, True, True])
    ticking(host, steady_seconds=2, check_deadline_seconds=100).check_instance("site-t2", "/")
    assert next(host.answers, None) is None


def test_an_instance_that_never_answers_fails_its_check_with_the_proxys_error() -> None:
    host = ProxyHost({}, answers=[False] * 10)
    with pytest.raises(CheckFailed, match="Connection refused"):
        ticking(host, steady_seconds=2, check_deadline_seconds=3).check_instance("site-t2", "/")
