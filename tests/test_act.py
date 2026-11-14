import pytest

from launchplane.act import Lanes
from launchplane.decide import LaneRoute, Outcome, ProxyNotRunning, RetireOutcome
from launchplane.proxy import CheckFailed, ProxyError
from launchplane.running_copy import CopyState, LaneCopy, RunningCopy

TARGET = LaneCopy(product="site", lane="testing", build="site:b")
NAME = "testing.site.test"


class FakeHost:
    def __init__(self) -> None:
        self.copies: dict[str, RunningCopy] = {}
        self.actions: list[str] = []

    def read_copies(self) -> list[RunningCopy]:
        return list(self.copies.values())

    def start_copy(self, copy: LaneCopy, name: str) -> None:
        self.copies[name] = RunningCopy(product=copy.product, lane=copy.lane, build=copy.build, name=name, state=CopyState.RUNNING)
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
        self.routes: dict[str, str] = {}
        self.failing: set[str] = set()

    def start(self) -> None:
        self.running = self.starts
        self.host.actions.append("start proxy")

    def read_lane_route(self, name: str) -> ProxyNotRunning | LaneRoute:
        if not self.running:
            return ProxyNotRunning()
        return LaneRoute(loaded=self.routes.get(name), written=self.routes.get(name))

    def await_route(self, name: str, copy_name: str | None) -> None:
        assert self.routes.get(name) == copy_name

    def write_route(self, name: str, copy_name: str | None) -> None:
        if copy_name is None:
            del self.routes[name]
        else:
            self.routes[name] = copy_name
        self.host.actions.append(f"route {copy_name}")

    def check_copy(self, copy_name: str) -> None:
        self.host.actions.append(f"check {copy_name}")
        if copy_name in self.failing:
            raise CheckFailed("Connection refused")


def lanes_with(proxy: FakeProxy) -> Lanes:
    return Lanes(proxy.host, proxy, name_copy=lambda copy: f"{copy.product}-{copy.lane}-new")


def test_a_failed_check_is_answered_as_check_failed_never_as_switched() -> None:
    proxy = FakeProxy(FakeHost())
    proxy.failing.add("site-testing-new")
    result = lanes_with(proxy).move(TARGET)
    assert (result.outcome, result.serving) == (Outcome.CHECK_FAILED, None)
    assert result.error is not None
    assert "Connection refused" in result.error
    assert "listening on :5678" in result.error


def test_a_copy_that_fails_its_check_is_removed_and_never_routed() -> None:
    proxy = FakeProxy(FakeHost())
    proxy.failing.add("site-testing-new")
    lanes_with(proxy).move(TARGET)
    assert proxy.host.actions == ["start site-testing-new", "check site-testing-new", "remove site-testing-new"]
    assert (proxy.routes, proxy.host.copies) == ({}, {})


def test_a_move_carries_out_what_was_decided_and_answers_switched() -> None:
    proxy = FakeProxy(FakeHost())
    result = lanes_with(proxy).move(TARGET)
    assert proxy.host.actions == ["start site-testing-new", "check site-testing-new", "route site-testing-new"]
    assert (result.outcome, result.serving) == (Outcome.SWITCHED, "site-testing-new")


def test_the_first_move_starts_the_proxy_then_reads_again_and_moves() -> None:
    proxy = FakeProxy(FakeHost(), running=False)
    result = lanes_with(proxy).move(TARGET)
    assert proxy.host.actions[0] == "start proxy"
    assert result.outcome == Outcome.SWITCHED


def test_a_proxy_that_does_not_run_after_starting_is_an_error_not_a_move() -> None:
    proxy = FakeProxy(FakeHost(), running=False, starts=False)
    with pytest.raises(ProxyError, match="still does not run"):
        lanes_with(proxy).move(TARGET)
    assert proxy.host.copies == {}


def test_retiring_carries_out_the_route_removal_before_the_copies() -> None:
    proxy = FakeProxy(FakeHost())
    lanes = lanes_with(proxy)
    lanes.move(TARGET)
    proxy.host.actions.clear()
    result = lanes.retire("site", "testing")
    assert proxy.host.actions == ["route None", "retire site-testing-new"]
    assert result.outcome == RetireOutcome.RETIRED
