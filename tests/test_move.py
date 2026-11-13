import json
from pathlib import PurePosixPath

import pytest

from launchplane.container_host import CommandResult, Service
from launchplane.move import Mover, Outcome, lane_name
from launchplane.name_switch import LOADED_ROUTES_URL, PROXY_NAME, CheckFailed, NameSwitch, NameSwitchError, routes_file
from launchplane.running_copy import CopyState, LaneCopy, RunningCopy

BUILD_A = LaneCopy(product="site", lane="testing", build="site:a")
BUILD_B = LaneCopy(product="site", lane="testing", build="site:b")
NAME = lane_name("site", "testing")


class FakeHost:
    def __init__(self) -> None:
        self.copies: dict[str, RunningCopy] = {}
        self.actions: list[str] = []

    def place(self, copy: LaneCopy, state: CopyState = CopyState.RUNNING) -> None:
        self.copies[copy.name] = RunningCopy(product=copy.product, lane=copy.lane, build=copy.build, name=copy.name, state=state)

    def read_copies(self) -> list[RunningCopy]:
        return list(self.copies.values())

    def start_copy(self, copy: LaneCopy) -> None:
        self.actions.append(f"start {copy.build}")
        self.place(copy)

    def remove_copy(self, name: str) -> None:
        if self.copies.pop(name, None) is not None:
            self.actions.append(f"remove {name}")

    def retire_copy(self, name: str) -> None:
        self.copies.pop(name)
        self.actions.append(f"retire {name}")

    def read_log_tail(self, name: str) -> str:
        return "listening on :5678"


class FakeSwitch:
    def __init__(self, failing: set[str] | None = None) -> None:
        self.serving: dict[str, str] = {}
        self.failing = failing or set()
        self.points: list[str] = []

    def ensure_ready(self) -> None:
        pass

    def read_serving(self) -> dict[str, str]:
        return dict(self.serving)

    def check_copy(self, copy_name: str) -> None:
        if copy_name in self.failing:
            raise CheckFailed("Connection refused")

    def point(self, name: str, copy_name: str) -> None:
        self.serving[name] = copy_name
        self.points.append(copy_name)


def lane_serving(copy: LaneCopy) -> tuple[FakeHost, FakeSwitch]:
    host, switch = FakeHost(), FakeSwitch()
    host.place(copy)
    switch.serving[NAME] = copy.name
    return host, switch


def test_the_old_copy_is_retired_only_after_the_name_points_at_the_new_one() -> None:
    host, switch = lane_serving(BUILD_A)
    result = Mover(host, switch).move(BUILD_B)
    assert (result.outcome, result.serving) == (Outcome.SWITCHED, BUILD_B.name)
    assert host.actions == ["start site:b", f"retire {BUILD_A.name}"]
    assert switch.serving == {NAME: BUILD_B.name}


def test_a_failed_check_switches_nothing_removes_the_copy_and_reports_its_error_and_log() -> None:
    host, switch = lane_serving(BUILD_A)
    switch.failing.add(BUILD_B.name)
    result = Mover(host, switch).move(BUILD_B)
    assert result.outcome == Outcome.CHECK_FAILED
    assert result.error is not None
    assert "Connection refused" in result.error
    assert "listening on :5678" in result.error
    assert switch.points == []
    assert set(host.copies) == {BUILD_A.name}


def test_asking_again_for_the_build_already_serving_changes_nothing() -> None:
    host, switch = lane_serving(BUILD_B)
    result = Mover(host, switch).move(BUILD_B)
    assert result.outcome == Outcome.UNCHANGED
    assert host.actions == []
    assert switch.points == []


def test_a_copy_left_running_before_the_switch_is_checked_and_switched_to_without_starting_another() -> None:
    host, switch = lane_serving(BUILD_A)
    host.place(BUILD_B)
    Mover(host, switch).move(BUILD_B)
    assert host.actions == [f"retire {BUILD_A.name}"]
    assert switch.serving == {NAME: BUILD_B.name}


def test_a_copy_left_stopped_before_the_switch_is_replaced_by_a_fresh_one() -> None:
    host, switch = lane_serving(BUILD_A)
    host.place(BUILD_B, CopyState.STOPPED)
    Mover(host, switch).move(BUILD_B)
    assert host.actions[:2] == [f"remove {BUILD_B.name}", "start site:b"]


def test_a_copy_left_by_an_abandoned_move_is_removed_by_a_move_elsewhere() -> None:
    host, switch = lane_serving(BUILD_A)
    host.place(BUILD_B)
    result = Mover(host, switch).move(BUILD_A)
    assert result.outcome == Outcome.CLEANED_UP
    assert set(host.copies) == {BUILD_A.name}


def test_an_old_copy_left_after_the_switch_is_retired_when_the_move_is_asked_again() -> None:
    host, switch = lane_serving(BUILD_B)
    host.place(BUILD_A)
    Mover(host, switch).move(BUILD_B)
    assert set(host.copies) == {BUILD_B.name}


def test_a_route_to_a_copy_that_no_longer_exists_is_not_taken_as_serving() -> None:
    host, switch = FakeHost(), FakeSwitch()
    switch.serving[NAME] = BUILD_B.name
    result = Mover(host, switch).move(BUILD_B)
    assert result.outcome == Outcome.SWITCHED
    assert host.actions == ["start site:b"]


def test_copies_in_other_lanes_are_never_touched() -> None:
    host, switch = lane_serving(BUILD_A)
    production = LaneCopy(product="site", lane="production", build="site:a")
    host.place(production)
    Mover(host, switch).move(BUILD_B)
    assert production.name in host.copies


def loaded_from(routes_file_text: str) -> str:
    http = json.loads(routes_file_text)["http"]
    routers = {f"{key}@file": router for key, router in http["routers"].items()}
    services = {f"{key}@file": service for key, service in http["services"].items()}
    return json.dumps({"routers": routers | INTERNAL_ROUTERS, "services": services | INTERNAL_SERVICES})


INTERNAL_ROUTERS = {"api@internal": {"rule": "PathPrefix(`/api`)", "service": "api@internal"}}
INTERNAL_SERVICES = {"api@internal": {"status": "enabled"}}


class ProxyHost:
    def __init__(self, serving: dict[str, str], answers: list[bool] | None = None, loads: bool = True) -> None:
        self.loaded = loaded_from(routes_file(serving))
        self.answers = iter(answers or [])
        self.loads = loads
        self.placed: dict[PurePosixPath, str] = {}

    def ensure_service(self, service: Service) -> None:
        pass

    def is_running(self, name: str) -> bool:
        return True

    def run_in(self, name: str, command: list[str]) -> CommandResult:
        assert name == PROXY_NAME
        if command[0] == "mv":
            if self.loads:
                self.loaded = loaded_from(self.placed[PurePosixPath(command[1])])
            return CommandResult(exit_code=0, output="")
        if command[-1] == LOADED_ROUTES_URL:
            return CommandResult(exit_code=0, output=self.loaded)
        answered = next(self.answers)
        return CommandResult(exit_code=0 if answered else 1, output="" if answered else "Connection refused")

    def place_file(self, name: str, path: PurePosixPath, content: str) -> None:
        self.placed[path] = content

    def read_log_tail(self, name: str) -> str:
        return "error while building configuration"


def ticking_switch(host: ProxyHost, **seconds: float) -> NameSwitch:
    clock = iter(range(1000))
    return NameSwitch(host, sleep=lambda _: None, clock=lambda: next(clock), **seconds)


def test_pointing_one_name_keeps_every_other_name_where_it_was() -> None:
    host = ProxyHost({"production.site.test": "site-p1"})
    ticking_switch(host).point(NAME, "site-t2")
    assert NameSwitch(host).read_serving() == {"production.site.test": "site-p1", NAME: "site-t2"}


def test_a_proxy_with_no_routes_serves_no_name() -> None:
    assert NameSwitch(ProxyHost({})).read_serving() == {}


def test_a_route_launchplane_did_not_write_is_an_error_not_overwritten() -> None:
    host = ProxyHost({})
    host.loaded = json.dumps({"routers": {"shop@file": {"rule": "PathPrefix(`/`)", "service": "shop"}}})
    with pytest.raises(NameSwitchError, match="did not write"):
        NameSwitch(host).point(NAME, "site-t2")
    assert host.placed == {}


def test_a_switch_the_proxy_never_loads_is_an_error_carrying_the_proxys_log() -> None:
    host = ProxyHost({NAME: "site-t1"}, loads=False)
    with pytest.raises(NameSwitchError, match=r"still sends it to site-t1[\s\S]*error while building"):
        ticking_switch(host, load_deadline_seconds=3).point(NAME, "site-t2")


def test_a_copy_must_answer_steadily_so_one_answer_between_failures_does_not_pass() -> None:
    host = ProxyHost({}, answers=[True, False, True, True, True])
    ticking_switch(host, steady_seconds=2, check_deadline_seconds=100).check_copy("site-t2")
    assert next(host.answers, None) is None


def test_a_copy_that_never_answers_fails_its_check_with_the_proxys_error() -> None:
    host = ProxyHost({}, answers=[False] * 10)
    with pytest.raises(CheckFailed, match="Connection refused"):
        ticking_switch(host, steady_seconds=2, check_deadline_seconds=3).check_copy("site-t2")
