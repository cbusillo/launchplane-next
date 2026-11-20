import io
import json
import os
import subprocess
import tarfile
import tempfile
import threading
import time
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

import pytest

from launchplane.container_host import ContainerHost, FileRead, HelperContainer
from launchplane.docker_answers import CommandResult
from launchplane.gate import DONE, GATE_FOLDER_NAME, Answer, GateError
from launchplane.instance import BACK_SOON, Conflicting, LaneRoute, LoadedRoute, RouteStatus, Unread, Verdict, WrittenRoute
from launchplane.proxy import (
    LOADED_ROUTES_URL,
    PROXY_NAME,
    ROUTES_FOLDER,
    VOLUME_FOLDER,
    CheckFailed,
    ProxyError,
    TraefikProxy,
    installed_text,
    instance_url,
    route_file_of,
    route_text,
)
from tests.pretend_host import GENERATION
from tests.recording_docker import ExecResult, RecordingContainer, RecordingDocker

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
        if "sh" in command:
            result = self.run_in_a_folder(command[command.index("sh") :])
            if self.loads_on_write:
                self.load()
            return result
        elif command[-1] == LOADED_ROUTES_URL:
            return CommandResult(exit_code=0, output=self.loaded) if self.reads_answer else CommandResult(1, "Connection refused")
        else:
            answered = next(self.answers)
            return CommandResult(exit_code=0 if answered else 1, output="" if answered else "Connection refused")
        if self.loads_on_write:
            self.load()
        return CommandResult(exit_code=0, output="")

    # The commands of the gate are run as they are, by a real shell, on a folder that stands for the proxy's.
    def run_in_a_folder(self, command: list[str]) -> CommandResult:
        with tempfile.TemporaryDirectory() as folder:
            for path, text in self.files.items():
                there = Path(folder) / path.relative_to(VOLUME_FOLDER)
                there.parent.mkdir(parents=True, exist_ok=True)
                there.write_text(text)
            ran = subprocess.run(  # noqa: S603
                [argument.replace(str(VOLUME_FOLDER), folder) for argument in command], capture_output=True, text=True, check=False
            )
            kept = (each for each in Path(folder).rglob("*") if each.is_file())
            self.files = {VOLUME_FOLDER / each.relative_to(folder): each.read_text() for each in kept}
        return CommandResult(exit_code=ran.returncode, output=(ran.stdout or ran.stderr).strip())

    def read_folder(self, container: str, folder: PurePosixPath) -> dict[str, FileRead]:
        assert container == PROXY_NAME
        return {path.name: FileRead(text, WRITTEN_AT) for path, text in self.files.items() if folder in path.parents}

    def read_log_tail(self, container: str) -> str:
        return f"{container}: {self.log}"


def routes_of(host: ProxyHost) -> dict[str, str]:
    return {path.stem: text for path, text in host.files.items() if path.parent == ROUTES_FOLDER}


def lane_route(host: ProxyHost, web_address: str = WEB_ADDRESS) -> LaneRoute:
    return TraefikProxy(host).read().route_of(web_address)


def sends_visitors_to(host: ProxyHost) -> dict[str, str]:
    return TraefikProxy(host).read().sends_visitors_to()


# A change of a lane's route that is no release, asked as its request's first attempt asks it: from the route as read.
def edit(proxy: TraefikProxy, web_address: str, container: str | None) -> str:
    return proxy.edit(web_address, GENERATION, 1, proxy.read().route_of(web_address).installed, container)


def ticking(host: ProxyHost, **seconds: float) -> TraefikProxy:
    clock = iter(range(1000))
    return TraefikProxy(host, sleep=lambda _: host.load(), clock=lambda: next(clock), **seconds)


def test_a_switch_asks_the_gate_to_rename_the_lanes_routes_file_to_the_route_of_its_candidate() -> None:
    host = ProxyHost({WEB_ADDRESS: "a"})
    proxy = TraefikProxy(host)
    assert proxy.switch(OTHER, GENERATION, 1, "b") == Answer(Verdict.COMMITTED, "b")
    assert (host.files[route_file_of(OTHER)], proxy.read().route_of(OTHER).written, sends_visitors_to(host)[OTHER]) == (
        installed_text(OTHER, "b", GENERATION, 1),
        "b",
        "b",
    )


def test_removing_a_lanes_route_leaves_what_the_gate_keeps_of_its_releases() -> None:
    host = ProxyHost({WEB_ADDRESS: "a"})
    proxy = TraefikProxy(host)
    proxy.switch(WEB_ADDRESS, GENERATION, 1, "b")
    assert edit(proxy, WEB_ADDRESS, None) == DONE
    assert (proxy.gate.verdict(WEB_ADDRESS, GENERATION), proxy.read().route_of(WEB_ADDRESS).written) == (
        Answer(Verdict.COMMITTED, "b"),
        None,
    )


def test_the_routes_folder_is_not_read_while_another_lanes_file_is_being_put_in_place() -> None:
    host = ProxyHost({WEB_ADDRESS: "a"})
    proxy = TraefikProxy(host)
    run_in, read_folder = host.run_in, host.read_folder
    touched: list[str] = []

    def run_and_say(container: str, command: list[str]) -> CommandResult:
        if command[0] != "flock":
            return run_in(container, command)
        touched.append("asked")
        time.sleep(0.1)
        touched.append("put in place")
        return run_in(container, command)

    def read_and_say(container: str, folder: PurePosixPath) -> dict[str, FileRead]:
        touched.append("read")
        return read_folder(container, folder)

    host.run_in, host.read_folder = run_and_say, read_and_say
    other_lane = threading.Thread(target=proxy.edit, args=(OTHER, GENERATION, 1, "", "b"))
    other_lane.start()
    while not touched:
        time.sleep(0.01)
    read = proxy.read().written
    other_lane.join(timeout=5)
    assert (touched, sorted(read)) == (["asked", "put in place", "read"], [OTHER, WEB_ADDRESS])


# The proxy's volume as a folder on this machine. Asked for a path, it hands over everything below it, as Docker does.
class VolumeContainer(RecordingContainer):
    volume: Path
    handed_over: list[str]

    def get_archive(self, path: str) -> tuple[list[bytes], dict[str, object]]:
        asked = self.volume / PurePosixPath(path).relative_to(VOLUME_FOLDER)
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w") as tar:
            tar.add(asked, arcname=asked.name)
            self.handed_over = tar.getnames()
        return [archive.getvalue()], {}


@pytest.mark.parametrize("status", ["running", "exited"])
def test_reading_the_routes_takes_none_of_the_thousands_of_files_the_gate_keeps_off_the_host(tmp_path: Path, status: str) -> None:
    route = tmp_path / route_file_of(WEB_ADDRESS).relative_to(VOLUME_FOLDER)
    route.parent.mkdir()
    route.write_text(route_text(WEB_ADDRESS, "site-t1"))
    os.utime(route, (WRITTEN_AT.timestamp(), WRITTEN_AT.timestamp()))
    (tmp_path / GATE_FOLDER_NAME).mkdir()
    for release in range(3000):
        (tmp_path / GATE_FOLDER_NAME / f"{WEB_ADDRESS}.{release}.armed").write_text("1 site-t1")
    docker = RecordingDocker()
    loaded = ExecResult(0, loaded_from({route_file_of(WEB_ADDRESS): route.read_text()}).encode())
    proxy = VolumeContainer(PROXY_NAME, {}, [], {}, status=status, answers=[loaded])
    proxy.volume, docker.containers.by_name[PROXY_NAME] = tmp_path, proxy
    seen = TraefikProxy(ContainerHost(docker.as_client(), "tcp://scratch:2376")).read()
    assert (seen.runs, seen.written, proxy.handed_over) == (
        status == "running",
        {WEB_ADDRESS: WrittenRoute("site-t1", WRITTEN_AT)},
        ["routes", f"routes/{WEB_ADDRESS}.yml"],
    )


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
    assert lane_route(ProxyHost({}, running=False)) == LaneRoute(loaded=None, file=None, proxy_runs=False)
    assert lane_route(ProxyHost({})) == LaneRoute(loaded=None, file=None)


def test_a_route_written_and_not_yet_loaded_reads_as_both() -> None:
    host = ProxyHost({WEB_ADDRESS: "site-t1"})
    host.files[route_file_of(WEB_ADDRESS)] = route_text(WEB_ADDRESS, "site-t2")
    route = lane_route(host)
    assert (route.loaded, route.written) == (LoadedRoute("site-t1", RouteStatus.ENABLED), "site-t2")


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
def test_a_lane_file_that_is_not_the_route_launchplane_writes_is_that_lanes_fault_and_every_other_lane_is_still_read(
    text: str,
) -> None:
    host = ProxyHost({OTHER: "site-p1"})
    host.files[route_file_of(WEB_ADDRESS)] = text
    seen = TraefikProxy(host).read()
    assert (seen.route_of(OTHER).written, seen.route_of(WEB_ADDRESS).written) == ("site-p1", None)
    unread = seen.route_of(WEB_ADDRESS).file
    assert isinstance(unread, Unread) and "not the route Launchplane writes" in str(unread)


FIRST_LINES = {
    "a comment the code before the gate wrote": "# launchplane.request=41\n",
    "none": "",
    "any other comment": "# launchplane.generation=01a1ef36-c51d-74d9-86a1-ff0199cf7a3d\n",
}


@pytest.mark.parametrize("stamp", FIRST_LINES.values(), ids=FIRST_LINES.keys())
def test_a_route_with_this_first_line_is_a_route_all_the_same_and_says_nothing_of_any_release(stamp: str) -> None:
    host = ProxyHost({})
    host.files[route_file_of(WEB_ADDRESS)] = stamp + route_text(WEB_ADDRESS, "site-t1")
    host.load()
    route = lane_route(host)
    assert (route.file, route.sends_visitors_to) == (WrittenRoute("site-t1", WRITTEN_AT), "site-t1")


SECOND_ROUTERS = {
    "to another instance, listed after": ("zz", "site-t2"),
    "to another instance, listed before": ("aa", "site-t2"),
    "to the same instance": ("zz", "site-t1"),
}


@pytest.mark.parametrize(("key", "target"), SECOND_ROUTERS.values(), ids=SECOND_ROUTERS.keys())
def test_a_second_router_for_a_web_address_reads_as_a_conflict_naming_both_and_is_never_one_target(key: str, target: str) -> None:
    host = ProxyHost({WEB_ADDRESS: "site-t1", OTHER: "site-p1"})
    loaded = json.loads(host.loaded)
    second = {f"{key}@file": loaded["routers"][f"{WEB_ADDRESS}@file"] | {"service": key}}
    loaded["routers"] = second | loaded["routers"] if key == "aa" else loaded["routers"] | second
    loaded["services"][f"{key}@file"] = {"loadBalancer": {"servers": [{"url": instance_url(target)}]}}
    host.loaded = json.dumps(loaded)
    seen = TraefikProxy(host).read()
    route = seen.route_of(WEB_ADDRESS)
    assert isinstance(route.loaded, Conflicting)
    assert {(each.router, each.container) for each in route.loaded.seen} == {
        (f"{WEB_ADDRESS}@file", "site-t1"),
        (f"{key}@file", target),
    }
    assert (route.sends_visitors_to, route.names, seen.sends_visitors_to()) == (None, {"site-t1", target}, {OTHER: "site-p1"})


def test_waiting_for_a_route_never_takes_one_of_two_routers_for_the_route_loaded() -> None:
    host = ProxyHost({WEB_ADDRESS: "site-t1"})
    loaded = json.loads(host.loaded)
    loaded["routers"]["zz@file"] = loaded["routers"][f"{WEB_ADDRESS}@file"]
    host.loaded = json.dumps(loaded)
    proxy = TraefikProxy(host, sleep=lambda _: None, clock=iter(range(1000)).__next__, load_deadline_seconds=3)
    with pytest.raises(ProxyError, match=f"has 2 answers where it must have one: the router {WEB_ADDRESS}@file to site-t1"):
        proxy.await_route(WEB_ADDRESS, "site-t1")


def test_a_route_the_proxy_has_disabled_reads_with_its_status_and_sends_no_visitors() -> None:
    host = ProxyHost({WEB_ADDRESS: "site-t1", OTHER: "site-p1"})
    loaded = json.loads(host.loaded)
    loaded["routers"][f"{WEB_ADDRESS}@file"]["status"] = "disabled"
    host.loaded = json.dumps(loaded)
    route = lane_route(host)
    assert (route.loaded, route.written) == (LoadedRoute("site-t1", RouteStatus.DISABLED), "site-t1")
    assert sends_visitors_to(host) == {OTHER: "site-p1"}


def test_writing_a_route_changes_only_that_lanes_file() -> None:
    host = ProxyHost({WEB_ADDRESS: "site-t1", OTHER: "site-p1"})
    other_file = host.files[route_file_of(OTHER)]
    assert edit(TraefikProxy(host), WEB_ADDRESS, "site-t2") == DONE
    assert routes_of(host) == {WEB_ADDRESS: installed_text(WEB_ADDRESS, "site-t2", GENERATION, 1), OTHER: other_file}
    assert edit(TraefikProxy(host), WEB_ADDRESS, None) == DONE
    assert routes_of(host) == {OTHER: other_file}


@pytest.mark.parametrize("container", ["site-t2", None])
def test_a_change_of_a_route_the_host_does_not_run_is_an_error_with_why_not_a_route_taken_as_changed(container: str | None) -> None:
    host = ProxyHost({WEB_ADDRESS: "site-t1"})
    proxy = TraefikProxy(host)
    read = proxy.read().route_of(WEB_ADDRESS).installed
    host.refused_commands.add("flock")
    with pytest.raises(GateError, match="Read-only file system"):
        proxy.edit(WEB_ADDRESS, GENERATION, 1, read, container)


def test_awaiting_a_route_returns_only_once_the_proxy_has_loaded_it() -> None:
    host = ProxyHost({WEB_ADDRESS: "site-t1"})
    host.loads_on_write = False
    proxy = ticking(host)
    assert edit(proxy, WEB_ADDRESS, None) == DONE
    assert proxy.read().sends_visitors_to() == {WEB_ADDRESS: "site-t1"}
    proxy.await_route(WEB_ADDRESS, None)
    assert proxy.read().sends_visitors_to() == {}


def test_awaiting_an_enabled_route_does_not_return_for_a_route_the_proxy_has_loaded_and_disabled() -> None:
    host = ProxyHost({WEB_ADDRESS: "site-t1"})
    loaded = json.loads(host.loaded)
    loaded["routers"][f"{WEB_ADDRESS}@file"]["status"] = "disabled"
    host.loaded = json.dumps(loaded)
    proxy = TraefikProxy(host, sleep=lambda _: None, clock=iter(range(1000)).__next__, load_deadline_seconds=3)
    proxy.await_route(WEB_ADDRESS, "site-t1")
    with pytest.raises(ProxyError, match=r"the proxy has loaded: the router \S+ to site-t1, disabled"):
        proxy.await_route(WEB_ADDRESS, "site-t1", enabled=True)


def test_a_route_the_proxy_never_loads_is_an_error_carrying_the_proxys_log() -> None:
    host = ProxyHost({WEB_ADDRESS: "site-t1"})
    host.loads_on_write = False
    host.files[route_file_of(WEB_ADDRESS)] = route_text(WEB_ADDRESS, "site-t2")
    proxy = TraefikProxy(host, sleep=lambda _: None, clock=iter(range(1000)).__next__, load_deadline_seconds=3)
    with pytest.raises(
        ProxyError, match=r"send testing.site.test to site-t2, and the proxy has loaded: the router \S+ to site-t1[\s\S]*error while"
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
    assert edit(TraefikProxy(host), WEB_ADDRESS, BACK_SOON) == DONE
    assert host.running == {PROXY_NAME, BACK_SOON}
    assert sends_visitors_to(host) == {WEB_ADDRESS: BACK_SOON, OTHER: "site-p1"}


def test_since_when_is_read_from_the_route_file_of_a_lane_in_maintenance_and_of_no_other() -> None:
    host = ProxyHost({WEB_ADDRESS: BACK_SOON, OTHER: "site-p1"})
    assert (lane_route(host).file, lane_route(host).back_soon_since) == (WrittenRoute(BACK_SOON, WRITTEN_AT), WRITTEN_AT)
    assert (lane_route(host, OTHER).written, lane_route(host, OTHER).back_soon_since) == ("site-p1", None)


def test_the_mark_of_maintenance_is_read_from_the_route_file_while_the_proxy_is_down() -> None:
    route = lane_route(ProxyHost({WEB_ADDRESS: BACK_SOON}, running=False))
    assert (route.in_maintenance, route.proxy_runs, route.loaded, route.back_soon_since) == (True, False, None, WRITTEN_AT)


def test_a_back_soon_page_whose_container_is_not_running_is_read_as_not_running() -> None:
    host = ProxyHost({WEB_ADDRESS: BACK_SOON})
    host.running.discard(BACK_SOON)
    assert lane_route(host).back_soon_runs is False
