import json
from pathlib import PurePosixPath

import pytest

from launchplane.container_host import CommandResult, Service
from launchplane.move import Mover, Outcome, RetireOutcome, lane_name
from launchplane.name_switch import (
    LOADED_ROUTES_URL,
    PROXY_NAME,
    ROUTES_FILE,
    STAGED_ROUTES_FILE,
    CheckFailed,
    NameSwitch,
    NameSwitchError,
    routes_file,
)
from launchplane.running_copy import CopyState, LaneCopy, RunningCopy

BUILD_A = LaneCopy(product="site", lane="testing", build="site:a")
BUILD_B = LaneCopy(product="site", lane="testing", build="site:b")
NAME = lane_name("site", "testing")
COPY_A = "site-testing-aaaaaaaaaaaa"
COPY_B = "site-testing-bbbbbbbbbbbb"


class FakeHost:
    def __init__(self) -> None:
        self.copies: dict[str, RunningCopy] = {}
        self.actions: list[str] = []

    def place(self, copy: LaneCopy, name: str, state: CopyState = CopyState.RUNNING) -> None:
        self.copies[name] = RunningCopy(product=copy.product, lane=copy.lane, build=copy.build, name=name, state=state)

    def read_copies(self) -> list[RunningCopy]:
        return list(self.copies.values())

    def start_copy(self, copy: LaneCopy, name: str) -> None:
        self.actions.append(f"start {copy.build}")
        self.place(copy, name)

    def copy_of(self, build: str) -> str:
        (name,) = (copy.name for copy in self.copies.values() if copy.build == build)
        return name

    def remove_copy(self, name: str) -> None:
        if self.copies.pop(name, None) is not None:
            self.actions.append(f"remove {name}")

    def retire_copy(self, name: str) -> None:
        self.copies.pop(name)
        self.actions.append(f"retire {name}")

    def read_log_tail(self, name: str) -> str:
        return f"{self.copies[name].build} listening on :5678"


class FakeSwitch:
    def __init__(self, host: FakeHost) -> None:
        self.host = host
        self.serving: dict[str, str] = {}
        self.failing_builds: set[str] = set()

    def ensure_ready_for(self, name: str) -> None:
        pass

    def read_serving(self) -> dict[str, str]:
        return dict(self.serving)

    def check_copy(self, copy_name: str) -> None:
        assert copy_name not in self.serving.values(), f"{copy_name} was checked while the name already sent requests to it"
        self.host.actions.append(f"check {copy_name}")
        if self.host.copies[copy_name].build in self.failing_builds:
            raise CheckFailed("Connection refused")

    def point(self, name: str, copy_name: str) -> None:
        self.serving[name] = copy_name
        self.host.actions.append(f"point {copy_name}")

    def unpoint(self, name: str) -> None:
        del self.serving[name]
        self.host.actions.append(f"unpoint {name}")


def lane_serving(copy: LaneCopy, name: str) -> tuple[FakeHost, FakeSwitch]:
    host = FakeHost()
    switch = FakeSwitch(host)
    host.place(copy, name)
    switch.serving[NAME] = name
    return host, switch


def test_the_old_copy_is_retired_only_after_the_name_points_at_the_new_one() -> None:
    host, switch = lane_serving(BUILD_A, COPY_A)
    result = Mover(host, switch).move(BUILD_B)
    new = host.copy_of(BUILD_B.build)
    assert host.actions == ["start site:b", f"check {new}", f"point {new}", f"retire {COPY_A}"]
    assert (result.outcome, result.serving) == (Outcome.SWITCHED, new)


def test_a_failed_check_switches_nothing_removes_the_copy_and_reports_its_error_and_log() -> None:
    host, switch = lane_serving(BUILD_A, COPY_A)
    switch.failing_builds.add(BUILD_B.build)
    result = Mover(host, switch).move(BUILD_B)
    assert result.outcome == Outcome.CHECK_FAILED
    assert result.error is not None
    assert "Connection refused" in result.error
    assert "listening on :5678" in result.error
    assert (switch.serving, set(host.copies)) == ({NAME: COPY_A}, {COPY_A})


def test_asking_again_for_the_build_already_serving_changes_nothing() -> None:
    host, switch = lane_serving(BUILD_B, COPY_B)
    result = Mover(host, switch).move(BUILD_B)
    assert result.outcome == Outcome.UNCHANGED
    assert host.actions == []


def test_a_copy_left_running_before_the_switch_is_checked_and_switched_to_without_starting_another() -> None:
    host, switch = lane_serving(BUILD_A, COPY_A)
    host.place(BUILD_B, COPY_B)
    result = Mover(host, switch).move(BUILD_B)
    assert host.actions == [f"check {COPY_B}", f"point {COPY_B}", f"retire {COPY_A}"]
    assert result.serving == COPY_B


def test_a_copy_left_stopped_before_the_switch_is_replaced_by_a_fresh_one() -> None:
    host, switch = lane_serving(BUILD_A, COPY_A)
    host.place(BUILD_B, COPY_B, CopyState.STOPPED)
    result = Mover(host, switch).move(BUILD_B)
    new = host.copy_of(BUILD_B.build)
    assert host.actions == [f"retire {COPY_B}", "start site:b", f"check {new}", f"point {new}", f"retire {COPY_A}"]
    assert result.serving == new


def test_a_copy_left_by_an_abandoned_move_is_removed_by_a_move_elsewhere() -> None:
    host, switch = lane_serving(BUILD_A, COPY_A)
    host.place(BUILD_B, COPY_B)
    result = Mover(host, switch).move(BUILD_A)
    assert result.outcome == Outcome.CLEANED_UP
    assert set(host.copies) == {COPY_A}


def test_an_old_copy_left_after_the_switch_is_stopped_gracefully_not_removed_by_force_when_asked_again() -> None:
    host, switch = lane_serving(BUILD_B, COPY_B)
    host.place(BUILD_A, COPY_A)
    Mover(host, switch).move(BUILD_B)
    assert host.actions == [f"retire {COPY_A}"]


def test_a_replacement_for_a_stopped_serving_copy_gets_no_request_before_it_passes_its_check() -> None:
    host = FakeHost()
    mover = Mover(host, FakeSwitch(host))
    mover.move(BUILD_B)
    stopped = host.copy_of(BUILD_B.build)
    host.place(BUILD_B, stopped, CopyState.STOPPED)
    host.actions.clear()
    result = mover.move(BUILD_B)
    new = result.serving
    assert host.actions == ["start site:b", f"check {new}", f"point {new}", f"retire {stopped}"]


def test_two_lanes_whose_product_and_lane_join_to_the_same_text_never_share_a_copy() -> None:
    first = LaneCopy(product="a-b", lane="c", build="site:a")
    second = LaneCopy(product="a", lane="b-c", build="site:a")
    host = FakeHost()
    mover = Mover(host, FakeSwitch(host))
    mover.move(first)
    mover.move(second)
    assert {(copy.product, copy.lane) for copy in host.copies.values()} == {("a-b", "c"), ("a", "b-c")}


def test_a_route_to_a_copy_that_no_longer_exists_is_not_taken_as_serving() -> None:
    host = FakeHost()
    switch = FakeSwitch(host)
    switch.serving[NAME] = COPY_B
    assert Mover(host, switch).move(BUILD_B).outcome == Outcome.SWITCHED


def test_copies_in_other_lanes_are_never_touched() -> None:
    host, switch = lane_serving(BUILD_A, COPY_A)
    production = LaneCopy(product="site", lane="production", build="site:a")
    host.place(production, "site-production-pppppppppppp")
    Mover(host, switch).move(BUILD_B)
    assert "site-production-pppppppppppp" in host.copies


def test_retiring_takes_the_name_away_before_stopping_any_copy_so_no_new_request_reaches_a_stopping_copy() -> None:
    host, switch = lane_serving(BUILD_A, COPY_A)
    host.place(BUILD_B, COPY_B)
    result = Mover(host, switch).retire("site", "testing")
    assert host.actions == [f"unpoint {NAME}", f"retire {COPY_A}", f"retire {COPY_B}"]
    assert (result.outcome, result.route_removed_to, result.retired_copies) == (RetireOutcome.RETIRED, COPY_A, [COPY_A, COPY_B])


def test_retiring_a_retired_lane_again_changes_nothing_and_says_so() -> None:
    host, switch = lane_serving(BUILD_A, COPY_A)
    mover = Mover(host, switch)
    mover.retire("site", "testing")
    host.actions.clear()
    result = mover.retire("site", "testing")
    assert (result.outcome, host.actions) == (RetireOutcome.UNCHANGED, [])


def test_retiring_a_lane_whose_route_is_gone_but_whose_copy_remains_finishes_by_retiring_the_copy() -> None:
    host, switch = lane_serving(BUILD_A, COPY_A)
    del switch.serving[NAME]
    result = Mover(host, switch).retire("site", "testing")
    assert (result.outcome, host.actions) == (RetireOutcome.RETIRED, [f"retire {COPY_A}"])


def test_a_name_whose_copy_is_gone_is_retired_by_the_product_and_lane_it_names() -> None:
    host = FakeHost()
    switch = FakeSwitch(host)
    switch.serving[NAME] = COPY_A
    result = Mover(host, switch).retire("site", "testing")
    assert (result.outcome, switch.serving, host.actions) == (RetireOutcome.RETIRED, {}, [f"unpoint {NAME}"])


def test_retiring_a_lane_leaves_every_other_lane_its_name_and_copies() -> None:
    host, switch = lane_serving(BUILD_A, COPY_A)
    production = LaneCopy(product="site", lane="production", build="site:a")
    host.place(production, "site-production-pppppppppppp")
    switch.serving["production.site.test"] = "site-production-pppppppppppp"
    Mover(host, switch).retire("site", "testing")
    assert switch.serving == {"production.site.test": "site-production-pppppppppppp"}
    assert set(host.copies) == {"site-production-pppppppppppp"}


def loaded_from(routes_file_text: str) -> str:
    http = json.loads(routes_file_text)["http"]
    routers = {f"{key}@file": router for key, router in http["routers"].items()}
    services = {f"{key}@file": service for key, service in http["services"].items()}
    return json.dumps({"routers": routers | INTERNAL_ROUTERS, "services": services | INTERNAL_SERVICES})


INTERNAL_ROUTERS = {"api@internal": {"rule": "PathPrefix(`/api`)", "service": "api@internal"}}
INTERNAL_SERVICES = {"api@internal": {"status": "enabled"}}


class ProxyHost:
    def __init__(self, serving: dict[str, str], answers: list[bool] | None = None, loads: bool = True) -> None:
        self.files = {ROUTES_FILE: routes_file(serving)}
        self.loaded = loaded_from(self.files[ROUTES_FILE])
        self.answers = iter(answers or [])
        self.loads = loads
        self.loads_on_write = loads
        self.log = "error while building configuration"

    def load_when_watching(self) -> None:
        if self.loads:
            self.loaded = loaded_from(self.files[ROUTES_FILE])

    def ensure_service(self, service: Service) -> None:
        pass

    def is_running(self, name: str) -> bool:
        return name == PROXY_NAME and self.loaded != ""

    def run_in(self, name: str, command: list[str]) -> CommandResult:
        assert name == PROXY_NAME
        if command[0] == "mv":
            self.files[PurePosixPath(command[2])] = self.files.pop(PurePosixPath(command[1]))
            if self.loads_on_write:
                self.loaded = loaded_from(self.files[ROUTES_FILE])
            return CommandResult(exit_code=0, output="")
        if command[-1] == LOADED_ROUTES_URL:
            return CommandResult(exit_code=0, output=self.loaded)
        answered = next(self.answers)
        return CommandResult(exit_code=0 if answered else 1, output="" if answered else "Connection refused")

    def read_file(self, name: str, path: PurePosixPath) -> str | None:
        assert name == PROXY_NAME
        return self.files.get(path)

    def place_file(self, name: str, path: PurePosixPath, content: str) -> None:
        assert name == PROXY_NAME
        self.files[path] = content

    def read_log_tail(self, name: str) -> str:
        return f"{name}: {self.log}"


def ticking_switch(host: ProxyHost, **seconds: float) -> NameSwitch:
    clock = iter(range(1000))
    return NameSwitch(host, sleep=lambda _: host.load_when_watching(), clock=lambda: next(clock), **seconds)


def test_a_route_written_before_a_crash_but_not_yet_loaded_is_waited_for_so_its_copy_is_not_removed() -> None:
    host = FakeHost()
    host.place(BUILD_A, COPY_A)
    host.place(BUILD_B, COPY_B)
    proxy = ProxyHost({NAME: COPY_A}, answers=[True] * 10)
    proxy.files[ROUTES_FILE] = routes_file({NAME: COPY_B})
    Mover(host, ticking_switch(proxy)).move(BUILD_A)
    proxy.load_when_watching()
    assert NameSwitch(proxy).read_serving()[NAME] in host.copies


def test_another_lanes_route_that_never_loads_does_not_stop_a_move_of_this_lane() -> None:
    host = FakeHost()
    host.place(BUILD_A, COPY_A)
    proxy = ProxyHost({NAME: COPY_A, "production.site.test": "site-p1"}, answers=[True] * 10, loads=False)
    proxy.files[ROUTES_FILE] = routes_file({NAME: COPY_A, "production.site.test": "site-p2"})
    proxy.loads_on_write = True
    clock = iter(range(1000))
    result = Mover(host, NameSwitch(proxy, sleep=lambda _: None, clock=lambda: next(clock))).move(BUILD_B)
    assert result.outcome == Outcome.SWITCHED
    assert NameSwitch(proxy).read_serving()[NAME] == host.copy_of(BUILD_B.build)


def test_pointing_one_name_keeps_every_other_name_where_the_proxy_sends_it_not_where_the_file_says() -> None:
    host = ProxyHost({"production.site.test": "site-p1"})
    host.files[ROUTES_FILE] = routes_file({"production.site.test": "site-p2"})
    ticking_switch(host).point(NAME, "site-t2")
    assert NameSwitch(host).read_serving() == {"production.site.test": "site-p1", NAME: "site-t2"}


def test_unpointing_one_name_keeps_every_other_name_where_the_proxy_sends_it() -> None:
    host = ProxyHost({NAME: "site-t1", "production.site.test": "site-p1"})
    host.files[ROUTES_FILE] = routes_file({NAME: "site-t1", "production.site.test": "site-p2"})
    ticking_switch(host).unpoint(NAME)
    assert NameSwitch(host).read_serving() == {"production.site.test": "site-p1"}


def test_a_proxy_with_no_routes_serves_no_name() -> None:
    assert NameSwitch(ProxyHost({})).read_serving() == {}


def test_a_route_launchplane_did_not_write_is_an_error_not_overwritten() -> None:
    host = ProxyHost({})
    host.loaded = json.dumps({"routers": {"shop@file": {"rule": "PathPrefix(`/`)", "service": "shop"}}})
    with pytest.raises(NameSwitchError, match="did not write"):
        NameSwitch(host).point(NAME, "site-t2")
    assert STAGED_ROUTES_FILE not in host.files
    assert host.files[ROUTES_FILE] == routes_file({})


def test_a_switch_the_proxy_never_loads_is_an_error_carrying_the_proxys_log() -> None:
    host = ProxyHost({NAME: "site-t1"}, loads=False)
    with pytest.raises(NameSwitchError, match=r"written to site-t2 and still sends it to site-t1[\s\S]*error while building"):
        ticking_switch(host, load_deadline_seconds=3).point(NAME, "site-t2")


def test_a_copy_must_answer_steadily_so_one_answer_between_failures_does_not_pass() -> None:
    host = ProxyHost({}, answers=[True, False, True, True, True])
    ticking_switch(host, steady_seconds=2, check_deadline_seconds=100).check_copy("site-t2")
    assert next(host.answers, None) is None


def test_a_copy_that_never_answers_fails_its_check_with_the_proxys_error() -> None:
    host = ProxyHost({}, answers=[False] * 10)
    with pytest.raises(CheckFailed, match="Connection refused"):
        ticking_switch(host, steady_seconds=2, check_deadline_seconds=3).check_copy("site-t2")
