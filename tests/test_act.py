from collections.abc import Callable

import pytest

from launchplane.act import MOST_STEPS, Lanes, Stopped
from launchplane.decide import LaneRoute, LoadedRoute, Outcome, ProxyNotRunning, RetireOutcome, RouteStatus
from launchplane.proxy import CheckFailed
from launchplane.running_copy import CopyState, LaneCopy, RunningCopy

BUILD_A = LaneCopy(product="site", lane="testing", build="site:a")
BUILD_B = LaneCopy(product="site", lane="testing", build="site:b")
NAME = "testing.site.test"


class FakeHost:
    def __init__(self) -> None:
        self.copies: dict[str, RunningCopy] = {}
        self.actions: list[str] = []

    def place(self, copy: LaneCopy, name: str, state: CopyState = CopyState.RUNNING) -> None:
        self.copies[name] = RunningCopy(product=copy.product, lane=copy.lane, build=copy.build, name=name, state=state)

    def read_copies(self) -> list[RunningCopy]:
        return list(self.copies.values())

    def start_copy(self, copy: LaneCopy, name: str) -> None:
        self.place(copy, name)
        self.actions.append(f"start {name}")

    def remove_copy(self, name: str) -> None:
        del self.copies[name]
        self.actions.append(f"remove {name}")

    def retire_copy(self, name: str) -> None:
        del self.copies[name]
        self.actions.append(f"retire {name}")

    def read_log_tail(self, name: str) -> str:
        return f"{self.copies[name].build} listening on :5678"


class FakeProxy:
    def __init__(self, host: FakeHost, running: bool = True, starts: bool = True) -> None:
        self.host = host
        self.running = running
        self.starts = starts
        self.loaded: dict[str, str] = {}
        self.written: dict[str, str] = {}
        self.failing: set[str] = set()
        self.while_awaiting: Callable[[], None] = lambda: None

    def start(self) -> None:
        self.running = self.starts
        self.host.actions.append("start proxy")

    def read_lane_route(self, name: str) -> ProxyNotRunning | LaneRoute:
        if not self.running:
            return ProxyNotRunning()
        loaded = self.loaded.get(name)
        return LaneRoute(None if loaded is None else LoadedRoute(loaded, RouteStatus.ENABLED), self.written.get(name))

    def await_route(self, name: str, copy_name: str | None) -> None:
        assert self.written.get(name) == copy_name, f"awaited {name} at {copy_name}, written to {self.written.get(name)}"
        self.loaded = dict(self.written)
        self.while_awaiting()

    def write_route(self, name: str, copy_name: str) -> None:
        self.written[name] = copy_name
        self.host.actions.append(f"route {copy_name}")

    def remove_route(self, name: str) -> None:
        del self.written[name]
        self.host.actions.append("unroute")

    def check_copy(self, copy_name: str) -> None:
        self.host.actions.append(f"check {copy_name}")
        if copy_name in self.failing:
            raise CheckFailed("Connection refused")


def numbered_names() -> Callable[[LaneCopy], str]:
    names = iter(range(1000))
    return lambda copy: f"{copy.product}-{copy.lane}-{next(names)}"


def lanes_serving(build: LaneCopy, copy_name: str) -> tuple[Lanes, FakeProxy]:
    proxy = FakeProxy(FakeHost())
    proxy.host.place(build, copy_name)
    proxy.loaded[NAME] = proxy.written[NAME] = copy_name
    return Lanes(proxy.host, proxy, name_copy=numbered_names()), proxy


def test_a_move_reads_again_after_each_step_and_switches_in_order() -> None:
    lanes, proxy = lanes_serving(BUILD_A, "a")
    result = lanes.move(BUILD_B)
    new = result.serving
    assert proxy.host.actions == [f"start {new}", f"check {new}", f"route {new}", "retire a"]
    assert (result.outcome, list(proxy.host.copies)) == (Outcome.SWITCHED, [new])


def test_a_copy_that_stops_while_its_route_is_awaited_is_replaced_not_kept_as_serving() -> None:
    lanes, proxy = lanes_serving(BUILD_A, "a")
    proxy.host.place(BUILD_B, "b")
    proxy.written[NAME] = "b"
    proxy.while_awaiting = lambda: proxy.host.place(BUILD_B, "b", CopyState.STOPPED)
    result = lanes.move(BUILD_B)
    assert result.outcome == Outcome.SWITCHED
    assert [(copy.name, copy.state) for copy in proxy.host.copies.values()] == [(result.serving, CopyState.RUNNING)]
    assert proxy.written[NAME] == result.serving


def test_a_failed_check_is_answered_as_check_failed_with_the_log_and_the_copy_removed() -> None:
    lanes, proxy = lanes_serving(BUILD_A, "a")
    proxy.failing.add("site-testing-0")
    result = lanes.move(BUILD_B)
    assert (result.outcome, result.serving) == (Outcome.CHECK_FAILED, "a")
    assert result.error is not None
    assert "Connection refused" in result.error
    assert "site:b listening on :5678" in result.error
    assert proxy.host.actions == ["start site-testing-0", "check site-testing-0", "remove site-testing-0"]
    assert proxy.written == {NAME: "a"}


def test_the_first_move_starts_the_proxy_then_moves() -> None:
    proxy = FakeProxy(FakeHost(), running=False)
    result = Lanes(proxy.host, proxy, name_copy=numbered_names()).move(BUILD_B)
    assert proxy.host.actions[0] == "start proxy"
    assert result.outcome == Outcome.SWITCHED


def test_a_proxy_that_never_runs_stops_the_move_with_an_error_not_a_loop() -> None:
    proxy = FakeProxy(FakeHost(), running=False, starts=False)
    with pytest.raises(Stopped, match="StartProxy"):
        Lanes(proxy.host, proxy).move(BUILD_B)
    assert proxy.host.copies == {}


def test_retiring_removes_the_route_before_any_copy() -> None:
    lanes, proxy = lanes_serving(BUILD_A, "a")
    result = lanes.retire("site", "testing")
    assert proxy.host.actions == ["unroute", "retire a"]
    assert (result.outcome, result.route_removed_to, result.retired_copies) == (RetireOutcome.RETIRED, "a", ["a"])


def test_a_retire_that_finishes_on_its_last_allowed_step_answers_retired_not_stopped() -> None:
    lanes, proxy = lanes_serving(BUILD_A, "a")
    for number in range(MOST_STEPS - 3):
        proxy.host.place(BUILD_A, f"b{number:02}")
    result = lanes.retire("site", "testing")
    assert len(proxy.host.actions) == MOST_STEPS - 1
    assert (result.outcome, proxy.host.copies) == (RetireOutcome.RETIRED, {})


def test_a_move_whose_loaded_route_is_disabled_stops_with_why_and_changes_nothing() -> None:
    lanes, proxy = lanes_serving(BUILD_A, "a")
    proxy.read_lane_route = lambda name: LaneRoute(LoadedRoute("a", RouteStatus.DISABLED), "a")
    with pytest.raises(Stopped, match="reports it disabled"):
        lanes.move(BUILD_B)
    assert proxy.host.actions == []
