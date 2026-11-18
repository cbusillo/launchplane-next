import json
import threading
import time
from datetime import UTC, datetime
from pathlib import PurePosixPath

import pytest

from launchplane.container_host import FileRead, HelperContainer
from launchplane.docker_answers import CommandResult
from launchplane.instance import BACK_SOON, LaneRoute, LoadedRoute, RouteStatus
from launchplane.proxy import (
    LOADED_ROUTES_URL,
    PROXY_NAME,
    CheckFailed,
    ProxyError,
    TraefikProxy,
    route_file_of,
    route_text,
)

WEB_ADDRESS = "testing.site.test"
OTHER = "production.site.test"
WRITTEN_AT = datetime(2026, 11, 16, 14, 2, tzinfo=UTC)
INTERNAL_ROUTERS = {"api@internal": {"rule": "PathPrefix(`/api`)", "service": "api@internal", "status": "enabled"}}
INTERNAL_SERVICES = {"api@internal": {"status": "enabled"}}


def loaded_from(files: dict[PurePosixPath, str]) -> str:
    routers: dict[str, object] = dict(INTERNAL_ROUTERS)
    services: dict[str, object] = dict(INTERNAL_SERVICES)
    for path, text in files.items():
        if path.suffix == ".yml":
            # As the proxy reads a routes file: a line that begins with # is a comment.
            http = json.loads("".join(line for line in text.splitlines() if not line.startswith("#")))["http"]
            routers |= {f"{key}@file": router | {"status": "enabled"} for key, router in http["routers"].items()}
            services |= {f"{key}@file": service for key, service in http["services"].items()}
    return json.dumps({"routers": routers, "services": services})


class ProxyHost:
    def __init__(self, routes: dict[str, str], answers: list[bool] | None = None, running: bool = True) -> None:
        self.files = {route_file_of(web_address): route_text(web_address, container) for web_address, container in routes.items()}
        self.loaded = loaded_from(self.files)
        self.answers = iter(answers or [])
        self.running = {PROXY_NAME, BACK_SOON} if running else {BACK_SOON}
        self.loads_on_write = True
        self.reads_answer = True
        self.log = "error while building configuration"
        self.refused_commands: set[str] = set()

    def load(self) -> None:
        self.loaded = loaded_from(self.files)

    def ensure_helper(self, helper: HelperContainer) -> None:
        self.running.add(helper.container)

    def is_running(self, container: str) -> bool:
        return container in self.running

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

    def read_folder(self, container: str, folder: PurePosixPath) -> dict[str, FileRead]:
        assert container == PROXY_NAME
        return {path.name: FileRead(text, WRITTEN_AT) for path, text in self.files.items() if path.parent == folder}

    def place_file(self, container: str, path: PurePosixPath, content: str) -> None:
        assert container == PROXY_NAME
        self.files[path] = content

    def read_log_tail(self, container: str) -> str:
        return f"{container}: {self.log}"


def lane_route(host: ProxyHost, web_address: str = WEB_ADDRESS) -> LaneRoute:
    return TraefikProxy(host).read().route_of(web_address)


def sends_visitors_to(host: ProxyHost) -> dict[str, str]:
    return TraefikProxy(host).read().sends_visitors_to()


def ticking(host: ProxyHost, **seconds: float) -> TraefikProxy:
    clock = iter(range(1000))
    return TraefikProxy(host, sleep=lambda _: host.load(), clock=lambda: next(clock), **seconds)


def test_a_route_says_which_request_wrote_it_and_one_written_by_hand_or_before_says_none() -> None:
    host = ProxyHost({WEB_ADDRESS: "a"})
    proxy = TraefikProxy(host)
    proxy.write_route(OTHER, "b", 41)
    seen = proxy.read()
    assert (seen.route_of(OTHER).written_by, seen.route_of(WEB_ADDRESS).written_by) == (41, None)
    assert (seen.route_of(OTHER).written, sends_visitors_to(host)[OTHER]) == ("b", "b")


def test_the_routes_folder_is_not_read_while_another_lanes_file_is_being_put_in_place() -> None:
    host = ProxyHost({WEB_ADDRESS: "a"})
    proxy = TraefikProxy(host)
    place, run_in, read_folder = host.place_file, host.run_in, host.read_folder
    touched: list[str] = []

    def place_slowly(container: str, path: PurePosixPath, content: str) -> None:
        touched.append("placed")
        time.sleep(0.1)
        place(container, path, content)

    def run_and_say(container: str, command: list[str]) -> CommandResult:
        touched.extend(["put in place"] if command[0] == "mv" else [])
        return run_in(container, command)

    def read_and_say(container: str, folder: PurePosixPath) -> dict[str, FileRead]:
        touched.append("read")
        return read_folder(container, folder)

    host.place_file, host.run_in, host.read_folder = place_slowly, run_and_say, read_and_say
    other_lane = threading.Thread(target=proxy.write_route, args=(OTHER, "b"))
    other_lane.start()
    while not touched:
        time.sleep(0.01)
    read = proxy.read_written()
    other_lane.join(timeout=5)
    assert (touched, sorted(read)) == (["placed", "put in place", "read"], [OTHER, WEB_ADDRESS])


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
        sends_visitors_to(host)


def test_a_failed_read_of_the_proxy_is_an_error_not_no_routes() -> None:
    host = ProxyHost({WEB_ADDRESS: "site-t1"})
    host.reads_answer = False
    with pytest.raises(ProxyError, match="Connection refused"):
        lane_route(host)


def test_a_proxy_with_no_routes_of_its_own_sends_no_web_address_anywhere() -> None:
    assert sends_visitors_to(ProxyHost({})) == {}


def test_a_proxy_that_is_not_running_is_told_apart_from_one_with_no_routes() -> None:
    assert lane_route(ProxyHost({}, running=False)) == LaneRoute(loaded=None, written=None, proxy_runs=False)
    assert lane_route(ProxyHost({})) == LaneRoute(loaded=None, written=None)


def test_a_route_written_and_not_yet_loaded_reads_as_both() -> None:
    host = ProxyHost({WEB_ADDRESS: "site-t1"})
    host.files[route_file_of(WEB_ADDRESS)] = route_text(WEB_ADDRESS, "site-t2")
    assert lane_route(host) == LaneRoute(LoadedRoute("site-t1", RouteStatus.ENABLED), written="site-t2")


def test_a_route_launchplane_did_not_write_is_an_error() -> None:
    host = ProxyHost({})
    host.loaded = json.dumps(
        {"routers": {"shop@file": {"rule": "PathPrefix(`/`)", "service": "shop", "status": "enabled"}}, "services": {}}
    )
    with pytest.raises(ProxyError, match="did not write"):
        lane_route(host)


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
        lane_route(host)


def test_a_route_the_proxy_has_disabled_reads_with_its_status_and_sends_no_visitors() -> None:
    host = ProxyHost({WEB_ADDRESS: "site-t1", OTHER: "site-p1"})
    loaded = json.loads(host.loaded)
    loaded["routers"][f"{WEB_ADDRESS}@file"]["status"] = "disabled"
    host.loaded = json.dumps(loaded)
    assert lane_route(host) == LaneRoute(LoadedRoute("site-t1", RouteStatus.DISABLED), "site-t1")
    assert sends_visitors_to(host) == {OTHER: "site-p1"}


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


def test_awaiting_a_route_returns_only_once_the_proxy_has_loaded_it() -> None:
    host = ProxyHost({WEB_ADDRESS: "site-t1"})
    host.loads_on_write = False
    proxy = ticking(host)
    proxy.remove_route(WEB_ADDRESS)
    assert proxy.read().sends_visitors_to() == {WEB_ADDRESS: "site-t1"}
    proxy.await_route(WEB_ADDRESS, None)
    assert proxy.read().sends_visitors_to() == {}


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


def test_showing_the_back_soon_page_starts_its_container_and_changes_only_that_lanes_route() -> None:
    host = ProxyHost({WEB_ADDRESS: "site-t1", OTHER: "site-p1"})
    host.running.discard(BACK_SOON)
    TraefikProxy(host).show_back_soon(WEB_ADDRESS)
    assert host.running == {PROXY_NAME, BACK_SOON}
    assert sends_visitors_to(host) == {WEB_ADDRESS: BACK_SOON, OTHER: "site-p1"}


def test_since_when_is_read_from_the_route_file_of_a_lane_in_maintenance_and_of_no_other() -> None:
    host = ProxyHost({WEB_ADDRESS: BACK_SOON, OTHER: "site-p1"})
    assert lane_route(host) == LaneRoute(LoadedRoute(BACK_SOON, RouteStatus.ENABLED), BACK_SOON, WRITTEN_AT)
    assert lane_route(host, OTHER) == LaneRoute(LoadedRoute("site-p1", RouteStatus.ENABLED), "site-p1")


def test_the_mark_of_maintenance_is_read_from_the_route_file_while_the_proxy_is_down() -> None:
    route = lane_route(ProxyHost({WEB_ADDRESS: BACK_SOON}, running=False))
    assert (route.in_maintenance, route.proxy_runs, route.loaded, route.back_soon_since) == (True, False, None, WRITTEN_AT)


def test_a_back_soon_page_whose_container_is_not_running_is_read_as_not_running() -> None:
    host = ProxyHost({WEB_ADDRESS: BACK_SOON})
    host.running.discard(BACK_SOON)
    assert lane_route(host).back_soon_runs is False
