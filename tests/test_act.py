from collections.abc import Callable

import pytest

from launchplane.act import MOST_STEPS, Lanes, Stopped
from launchplane.container_host import OnHost
from launchplane.data_set import DatabaseServer
from launchplane.decide import FreshNames, LaneRoute, LoadedRoute, Outcome, ProxyNotRunning, RetireOutcome, RouteStatus
from launchplane.instance import DataSet, InstanceState, LaneBuild, RunningInstance, RunningJob, job_container_of
from launchplane.product import Database, Job, Product
from launchplane.proxy import CheckFailed

BUILD_A = LaneBuild(product="site", lane="testing", build="site:a")
BUILD_B = LaneBuild(product="site", lane="testing", build="site:b")
WEB_ADDRESS = "testing.site.test"
WITH_DATA = Product(health_path="/health", database=Database(image="postgres:18"), job=Job(command=["job"]))


class Killed(Exception):
    pass


class FakeHost:
    def __init__(self) -> None:
        self.instances: dict[str, RunningInstance] = {}
        self.jobs: dict[str, RunningJob] = {}
        self.data_sets: dict[str, DataSet] = {}
        self.actions: list[str] = []
        self.most_jobs_running = 0
        self.killed_after: int | None = None

    def record(self, action: str) -> None:
        self.actions.append(action)
        if len(self.actions) == self.killed_after:
            raise Killed(f"killed after {action}")

    def place(self, target: LaneBuild, container: str, state: InstanceState = InstanceState.RUNNING) -> None:
        self.instances[container] = RunningInstance(
            product=target.product, lane=target.lane, build=target.build, container=container, state=state
        )

    def read(self) -> OnHost:
        return OnHost(list(self.instances.values()), list(self.jobs.values()), list(self.data_sets.values()))

    def start_instance(self, target: LaneBuild, container: str, data_set: str | None) -> None:
        self.place(target, container)
        self.instances[container] = self.instances[container].model_copy(update={"data_set": data_set})
        self.record(f"start {container}")

    def start_job(self, target: LaneBuild, instance: str, data_set: str | None, command: list[str]) -> None:
        container = job_container_of(instance)
        self.jobs[container] = RunningJob(
            product=target.product,
            lane=target.lane,
            build=target.build,
            container=container,
            state=InstanceState.RUNNING,
            instance=instance,
        )
        self.most_jobs_running = max(self.most_jobs_running, len(self.jobs))
        self.record(f"start job {instance} on {data_set} with {' '.join(command)}")

    def ensure_data_set(self, product: str, lane: str, data_set: str, server: DatabaseServer) -> None:
        self.data_sets[data_set] = DataSet(
            product=product,
            lane=lane,
            data_set=data_set,
            network_is_quiet=True,
            has_volume=True,
            database_container=f"database-{data_set}",
            database_state=InstanceState.RUNNING,
        )
        self.record(f"make {data_set} with {server.image}")

    def await_database(self, container: str, server: DatabaseServer) -> None:
        self.record(f"await {container} with {server.ready_command[0]}")

    def delete_data_set(self, data_set: str) -> None:
        del self.data_sets[data_set]
        self.record(f"delete {data_set}")

    def remove_instance(self, container: str) -> None:
        del self.instances[container]
        self.record(f"remove {container}")

    def retire_instance(self, container: str) -> None:
        if container in self.jobs:
            del self.jobs[container]
        else:
            del self.instances[container]
        self.record(f"retire {container}")

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
        self.host.record("start proxy")

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
        self.host.record(f"route {container}")

    def remove_route(self, web_address: str) -> None:
        del self.written[web_address]
        self.host.record("unroute")

    def check_instance(self, container: str, health_path: str) -> None:
        self.host.record(f"check {container}{health_path}")
        if container in self.failing:
            raise CheckFailed("Connection refused")


def numbered_names() -> Callable[[LaneBuild], FreshNames]:
    numbers = iter(range(1000))
    return lambda target: FreshNames(f"{target.product}-{target.lane}-{next(numbers)}", f"data-{next(numbers)}")


def lanes_serving(build: LaneBuild, container: str) -> tuple[Lanes, FakeProxy]:
    proxy = FakeProxy(FakeHost())
    proxy.host.place(build, container)
    proxy.loaded[WEB_ADDRESS] = proxy.written[WEB_ADDRESS] = container
    return Lanes(proxy.host, proxy, name_fresh=numbered_names()), proxy


def test_a_move_reads_again_after_each_step_and_switches_in_order() -> None:
    lanes, proxy = lanes_serving(BUILD_A, "a")
    result = lanes.move(BUILD_B)
    new = result.container_receiving_visitors
    assert proxy.host.actions == [f"start {new}", f"check {new}/", f"route {new}", "retire a"]
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
    assert proxy.host.actions == ["start site-testing-0", "check site-testing-0/", "remove site-testing-0"]
    assert proxy.written == {WEB_ADDRESS: "a"}


def test_the_first_move_starts_the_proxy_then_moves() -> None:
    proxy = FakeProxy(FakeHost(), running=False)
    result = Lanes(proxy.host, proxy, name_fresh=numbered_names()).move(BUILD_B)
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


def lanes_on(proxy: FakeProxy) -> Lanes:
    return Lanes(proxy.host, proxy, read_product=lambda _product: WITH_DATA, name_fresh=numbered_names())


def lane_brought_up_with_a() -> FakeProxy:
    proxy = FakeProxy(FakeHost())
    lanes_on(proxy).move(BUILD_A)
    proxy.host.actions.clear()
    return proxy


def lane_end_state(proxy: FakeProxy) -> tuple[list[str | None], list[str | None], list[str]]:
    host = proxy.host
    routed = host.instances[proxy.written[WEB_ADDRESS]]
    assert [job.instance for job in host.jobs.values()] == [routed.container]
    return [instance.build for instance in host.instances.values()], [routed.data_set], list(host.data_sets)


def test_a_lane_with_data_is_brought_up_data_set_first_and_its_job_starts_only_once_it_receives_visitors() -> None:
    proxy = FakeProxy(FakeHost())
    result = lanes_on(proxy).move(BUILD_A)
    new = result.container_receiving_visitors
    assert (result.outcome, result.data_set) == (Outcome.SWITCHED, "data-1")
    assert proxy.host.actions == [
        "make data-1 with postgres:18",
        "await database-data-1 with pg_isready",
        f"start {new}",
        f"check {new}/health",
        f"route {new}",
        f"start job {new} on data-1 with job",
    ]


def test_a_release_keeps_the_data_set_and_stops_the_old_job_before_the_new_one_starts() -> None:
    proxy = lane_brought_up_with_a()
    old = proxy.written[WEB_ADDRESS]
    result = lanes_on(proxy).move(BUILD_B)
    new = result.container_receiving_visitors
    assert proxy.host.actions == [
        "await database-data-1 with pg_isready",
        f"start {new}",
        f"check {new}/health",
        f"route {new}",
        f"retire {job_container_of(old)}",
        f"start job {new} on data-1 with job",
        f"retire {old}",
    ]
    assert result.data_set == "data-1"
    assert proxy.host.most_jobs_running == 1


def uninterrupted_steps(proxy: FakeProxy, target: LaneBuild) -> int:
    lanes_on(proxy).move(target)
    return len(proxy.host.actions)


def test_a_bring_up_killed_after_any_step_reaches_the_same_end_when_asked_again() -> None:
    for killed_after in range(1, uninterrupted_steps(FakeProxy(FakeHost()), BUILD_A)):
        proxy = FakeProxy(FakeHost())
        proxy.host.killed_after = killed_after
        with pytest.raises(Killed):
            lanes_on(proxy).move(BUILD_A)
        proxy.host.killed_after = None
        lanes_on(proxy).move(BUILD_A)
        assert lane_end_state(proxy) == (["site:a"], ["data-1"], ["data-1"]), proxy.host.actions


def test_a_release_killed_after_any_step_reaches_the_same_end_on_the_same_data_with_one_job_at_a_time() -> None:
    for killed_after in range(1, uninterrupted_steps(lane_brought_up_with_a(), BUILD_B)):
        proxy = lane_brought_up_with_a()
        proxy.host.killed_after = killed_after
        with pytest.raises(Killed):
            lanes_on(proxy).move(BUILD_B)
        proxy.host.killed_after = None
        lanes_on(proxy).move(BUILD_B)
        assert lane_end_state(proxy) == (["site:b"], ["data-1"], ["data-1"]), proxy.host.actions
        assert proxy.host.most_jobs_running == 1


def test_retiring_a_lane_with_data_stops_its_job_first_and_keeps_the_data_set_unless_named() -> None:
    proxy = lane_brought_up_with_a()
    instance = proxy.written[WEB_ADDRESS]
    kept = lanes_on(proxy).retire("site", "testing")
    assert proxy.host.actions == ["unroute", f"retire {job_container_of(instance)}", f"retire {instance}"]
    assert (kept.deleted_data_set, kept.kept_data_sets) == (None, ["data-1"])
    deleted = lanes_on(proxy).retire("site", "testing", delete_data_set="data-1")
    assert (deleted.outcome, deleted.deleted_data_set, deleted.kept_data_sets) == (RetireOutcome.RETIRED, "data-1", [])


def test_naming_a_data_set_of_another_lane_deletes_nothing() -> None:
    proxy = lane_brought_up_with_a()
    result = lanes_on(proxy).retire("site", "other", delete_data_set="data-1")
    assert (result.outcome, result.deleted_data_set) == (RetireOutcome.UNCHANGED, None)
    assert list(proxy.host.data_sets) == ["data-1"]
