from collections.abc import Callable

import pytest

from launchplane.act import MOST_STEPS, Lanes, Stopped
from launchplane.decide import LaneRoute, LoadedRoute, Outcome, ProxyNotRunning, RetireOutcome, RouteStatus
from launchplane.instance import InstanceState, LaneBuild, RunningInstance
from launchplane.proxy import CheckFailed

BUILD_A = LaneBuild(product="site", lane="testing", build="site:a")
BUILD_B = LaneBuild(product="site", lane="testing", build="site:b")
WEB_ADDRESS = "testing.site.test"


class FakeHost:
    def __init__(self) -> None:
        self.instances: dict[str, RunningInstance] = {}
        self.actions: list[str] = []

    def place(self, target: LaneBuild, container: str, state: InstanceState = InstanceState.RUNNING) -> None:
        self.instances[container] = RunningInstance(
            product=target.product, lane=target.lane, build=target.build, container=container, state=state
        )

    def read_instances(self) -> list[RunningInstance]:
        return list(self.instances.values())

    def start_instance(self, target: LaneBuild, container: str) -> None:
        self.place(target, container)
        self.actions.append(f"start {container}")

    def remove_instance(self, container: str) -> None:
        del self.instances[container]
        self.actions.append(f"remove {container}")

    def retire_instance(self, container: str) -> None:
        del self.instances[container]
        self.actions.append(f"retire {container}")

    def read_log_tail(self, container: str) -> str:
        return f"{self.instances[container].build} listening on :5678"


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

    def read_lane_route(self, web_address: str) -> ProxyNotRunning | LaneRoute:
        if not self.running:
            return ProxyNotRunning()
        loaded = self.loaded.get(web_address)
        return LaneRoute(None if loaded is None else LoadedRoute(loaded, RouteStatus.ENABLED), self.written.get(web_address))

    def await_route(self, web_address: str, container: str | None) -> None:
        written = self.written.get(web_address)
        assert written == container, f"awaited {web_address} at {container}, written to {written}"
        self.loaded = dict(self.written)
        self.while_awaiting()

    def write_route(self, web_address: str, container: str) -> None:
        self.written[web_address] = container
        self.host.actions.append(f"route {container}")

    def remove_route(self, web_address: str) -> None:
        del self.written[web_address]
        self.host.actions.append("unroute")

    def check_instance(self, container: str) -> None:
        self.host.actions.append(f"check {container}")
        if container in self.failing:
            raise CheckFailed("Connection refused")


def numbered_containers() -> Callable[[LaneBuild], str]:
    numbers = iter(range(1000))
    return lambda target: f"{target.product}-{target.lane}-{next(numbers)}"


def lanes_serving(build: LaneBuild, container: str) -> tuple[Lanes, FakeProxy]:
    proxy = FakeProxy(FakeHost())
    proxy.host.place(build, container)
    proxy.loaded[WEB_ADDRESS] = proxy.written[WEB_ADDRESS] = container
    return Lanes(proxy.host, proxy, name_container=numbered_containers()), proxy


def test_a_move_reads_again_after_each_step_and_switches_in_order() -> None:
    lanes, proxy = lanes_serving(BUILD_A, "a")
    result = lanes.move(BUILD_B)
    new = result.container_receiving_visitors
    assert proxy.host.actions == [f"start {new}", f"check {new}", f"route {new}", "retire a"]
    assert (result.outcome, list(proxy.host.instances)) == (Outcome.SWITCHED, [new])


def test_an_instance_that_stops_while_its_route_is_awaited_is_replaced_not_kept_receiving_visitors() -> None:
    lanes, proxy = lanes_serving(BUILD_A, "a")
    proxy.host.place(BUILD_B, "b")
    proxy.written[WEB_ADDRESS] = "b"
    proxy.while_awaiting = lambda: proxy.host.place(BUILD_B, "b", InstanceState.STOPPED)
    result = lanes.move(BUILD_B)
    assert result.outcome == Outcome.SWITCHED
    assert [(instance.container, instance.state) for instance in proxy.host.instances.values()] == [
        (result.container_receiving_visitors, InstanceState.RUNNING)
    ]
    assert proxy.written[WEB_ADDRESS] == result.container_receiving_visitors


def test_a_failed_check_is_answered_as_check_failed_with_the_log_and_the_instance_removed() -> None:
    lanes, proxy = lanes_serving(BUILD_A, "a")
    proxy.failing.add("site-testing-0")
    result = lanes.move(BUILD_B)
    assert (result.outcome, result.container_receiving_visitors) == (Outcome.CHECK_FAILED, "a")
    assert result.error is not None
    assert "Connection refused" in result.error
    assert "site:b listening on :5678" in result.error
    assert proxy.host.actions == ["start site-testing-0", "check site-testing-0", "remove site-testing-0"]
    assert proxy.written == {WEB_ADDRESS: "a"}


def test_the_first_move_starts_the_proxy_then_moves() -> None:
    proxy = FakeProxy(FakeHost(), running=False)
    result = Lanes(proxy.host, proxy, name_container=numbered_containers()).move(BUILD_B)
    assert proxy.host.actions[0] == "start proxy"
    assert result.outcome == Outcome.SWITCHED


def test_a_proxy_that_never_runs_stops_the_move_with_an_error_not_a_loop() -> None:
    proxy = FakeProxy(FakeHost(), running=False, starts=False)
    with pytest.raises(Stopped, match="StartProxy"):
        Lanes(proxy.host, proxy).move(BUILD_B)
    assert proxy.host.instances == {}


def test_retiring_removes_the_route_before_any_instance() -> None:
    lanes, proxy = lanes_serving(BUILD_A, "a")
    result = lanes.retire("site", "testing")
    assert proxy.host.actions == ["unroute", "retire a"]
    assert (result.outcome, result.route_removed_to_container, result.retired_containers) == (RetireOutcome.RETIRED, "a", ["a"])


def test_a_retire_that_finishes_on_its_last_allowed_step_answers_retired_not_stopped() -> None:
    lanes, proxy = lanes_serving(BUILD_A, "a")
    for number in range(MOST_STEPS - 3):
        proxy.host.place(BUILD_A, f"b{number:02}")
    result = lanes.retire("site", "testing")
    assert len(proxy.host.actions) == MOST_STEPS - 1
    assert (result.outcome, proxy.host.instances) == (RetireOutcome.RETIRED, {})


def test_a_move_whose_loaded_route_is_disabled_stops_with_why_and_changes_nothing() -> None:
    lanes, proxy = lanes_serving(BUILD_A, "a")
    proxy.read_lane_route = lambda web_address: LaneRoute(LoadedRoute("a", RouteStatus.DISABLED), "a")
    with pytest.raises(Stopped, match="reports it disabled"):
        lanes.move(BUILD_B)
    assert proxy.host.actions == []
