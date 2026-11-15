from collections.abc import Callable

import pytest

from launchplane.act import MOST_STEPS, Lanes, Stopped
from launchplane.container_host import DatabaseLink, OnHost, PartStopped
from launchplane.data_set import DatabaseServer
from launchplane.decide import (
    FreshNames,
    LaneRoute,
    LoadedRoute,
    Outcome,
    ProxyNotRunning,
    RetireOutcome,
    RouteStatus,
    lane_problems,
)
from launchplane.instance import (
    LANE_NETWORK,
    DataSet,
    DataSetDatabase,
    DataSetNetwork,
    HostNetwork,
    InstanceState,
    LaneBuild,
    RunningInstance,
    RunningJob,
    job_container_of,
)
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
        self.networks: dict[str, bool] = {LANE_NETWORK: True}
        self.ready_databases: set[str] = set()
        self.probes: list[str] = []
        self.actions: list[str] = []
        self.most_jobs_running = 0
        self.killed_after: int | None = None
        self.jobs_that_stop: set[str] = set()

    def record(self, action: str) -> None:
        self.actions.append(action)
        if len(self.actions) == self.killed_after:
            raise Killed(f"killed after {action}")

    def place(
        self, target: LaneBuild, container: str, state: InstanceState = InstanceState.RUNNING, link: DatabaseLink | None = None
    ) -> None:
        self.instances[container] = RunningInstance(
            product=target.product,
            lane=target.lane,
            build=target.build,
            container=container,
            state=state,
            networks=(LANE_NETWORK,) if link is None else (LANE_NETWORK, link.network),
            data_set=None if link is None else link.data_set,
        )

    def attach(self, container: str, network: str, *, quiet: bool) -> None:
        self.networks[network] = quiet
        found = self.instances[container]
        self.instances[container] = found.model_copy(update={"networks": (*found.networks, network)})

    def read(self) -> OnHost:
        networks = [HostNetwork(name=name, is_quiet=quiet) for name, quiet in self.networks.items()]
        return OnHost(list(self.instances.values()), list(self.jobs.values()), list(self.data_sets.values()), networks)

    def start_instance(self, target: LaneBuild, container: str, link: DatabaseLink | None) -> None:
        self.place(target, container, link=link)
        self.record(f"start {container}")

    def start_job(self, target: LaneBuild, instance: str, link: DatabaseLink | None, command: list[str]) -> None:
        container = job_container_of(instance)
        self.jobs[container] = RunningJob(
            product=target.product,
            lane=target.lane,
            build=target.build,
            container=container,
            state=InstanceState.RUNNING,
            networks=(LANE_NETWORK,) if link is None else (link.network,),
            running_for_seconds=0.0,
            instance=instance,
        )
        self.most_jobs_running = max(self.most_jobs_running, len(self.jobs))
        self.record(f"start job {instance} on {None if link is None else link.network} with {' '.join(command)}")

    def await_job(self, container: str, steady_seconds: float) -> None:
        self.record(f"await job {container}")
        if self.jobs[container].build in self.jobs_that_stop:
            self.jobs[container] = self.jobs[container].model_copy(
                update={"state": InstanceState.RESTARTING, "running_for_seconds": None}
            )
            raise PartStopped(f"The job {container} is not running: it stopped before it had run {steady_seconds:g} s.")
        self.jobs[container] = self.jobs[container].model_copy(update={"running_for_seconds": steady_seconds})

    def change_data_set(self, product: str, lane: str, data_set: str, **parts: object) -> None:
        found = self.data_sets.get(data_set) or DataSet(
            product=product, lane=lane, data_set=data_set, networks=(), volumes=(), databases=()
        )
        self.data_sets[data_set] = found.model_copy(update=parts)

    def create_data_set_network(self, product: str, lane: str, data_set: str) -> None:
        self.networks[f"network-{data_set}"] = True
        self.change_data_set(product, lane, data_set, networks=(DataSetNetwork(name=f"network-{data_set}", is_quiet=True),))
        self.record(f"network {data_set}")

    def create_data_set_volume(self, product: str, lane: str, data_set: str) -> None:
        self.change_data_set(product, lane, data_set, volumes=(f"volume-{data_set}",))
        self.record(f"volume {data_set}")

    def create_database(self, product: str, lane: str, data_set: str, network: str, volume: str, server: DatabaseServer) -> None:
        database = DataSetDatabase(container=f"database-{data_set}", state=InstanceState.RUNNING, networks=(network,))
        self.change_data_set(product, lane, data_set, databases=(database,))
        self.record(f"database {data_set} on {network} and {volume} from {server.image}")

    def start_database(self, container: str) -> None:
        self.record(f"start database {container}")

    def await_database(self, container: str, server: DatabaseServer) -> None:
        self.ready_databases.add(container)
        self.record(f"await {container} with {server.ready_command[0]}")

    def database_accepts_connections(self, container: str, server: DatabaseServer) -> bool:
        self.probes.append(f"{container} with {server.ready_command[0]}")
        return container in self.ready_databases

    def delete_data_set(self, found: DataSet) -> None:
        del self.data_sets[found.data_set]
        self.record(f"delete {found.data_set}")

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
        part = self.jobs.get(container) or self.instances[container]
        return f"{part.build} listening on :5678"


class FakeProxy:
    def __init__(self, host: FakeHost, running: bool = True, starts: bool = True) -> None:
        self.host = host
        self.running = running
        self.starts = starts
        self.loaded: dict[str, str] = {}
        self.written: dict[str, str] = {}
        self.failing: set[str] = set()
        self.while_awaiting: Callable[[], None] = lambda: None
        self.while_checking: Callable[[str], None] = lambda _container: None

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
        self.while_checking(container)
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


def lane_end_state(proxy: FakeProxy, target: LaneBuild) -> tuple[list[str | None], list[str | None], list[str]]:
    host = proxy.host
    routed = host.instances[proxy.written[WEB_ADDRESS]]
    assert lane_problems(lanes_on(proxy).read("site", "testing", WITH_DATA.database), target, WITH_DATA) == []
    return [instance.build for instance in host.instances.values()], [routed.data_set], list(host.data_sets)


def test_a_lane_with_data_is_brought_up_data_set_first_and_its_job_starts_only_once_it_receives_visitors() -> None:
    proxy = FakeProxy(FakeHost())
    result = lanes_on(proxy).move(BUILD_A)
    new = proxy.written[WEB_ADDRESS]
    assert (result.outcome, result.data_set) == (Outcome.SWITCHED, "data-1")
    assert proxy.host.actions == [
        "network data-1",
        "volume data-1",
        "database data-1 on network-data-1 and volume-data-1 from postgres:18",
        "await database-data-1 with pg_isready",
        f"start {new}",
        f"check {new}/health",
        f"route {new}",
        f"start job {new} on network-data-1 with job",
        f"await job {job_container_of(new)}",
    ]


def test_a_release_keeps_the_data_set_and_stops_the_old_job_before_the_new_one_starts() -> None:
    proxy = lane_brought_up_with_a()
    old = proxy.written[WEB_ADDRESS]
    result = lanes_on(proxy).move(BUILD_B)
    new = proxy.written[WEB_ADDRESS]
    assert proxy.host.actions == [
        f"start {new}",
        f"check {new}/health",
        f"route {new}",
        f"retire {job_container_of(old)}",
        f"start job {new} on network-data-1 with job",
        f"await job {job_container_of(new)}",
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
        assert lane_end_state(proxy, BUILD_A) == (["site:a"], ["data-1"], ["data-1"]), proxy.host.actions


def test_a_release_killed_after_any_step_reaches_the_same_end_on_the_same_data_with_one_job_at_a_time() -> None:
    for killed_after in range(1, uninterrupted_steps(lane_brought_up_with_a(), BUILD_B)):
        proxy = lane_brought_up_with_a()
        proxy.host.killed_after = killed_after
        with pytest.raises(Killed):
            lanes_on(proxy).move(BUILD_B)
        proxy.host.killed_after = None
        lanes_on(proxy).move(BUILD_B)
        assert lane_end_state(proxy, BUILD_B) == (["site:b"], ["data-1"], ["data-1"]), proxy.host.actions
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


def test_a_release_whose_job_exits_at_once_answers_job_failed_with_its_log_not_switched() -> None:
    proxy = lane_brought_up_with_a()
    proxy.host.jobs_that_stop.add(BUILD_B.build)
    result = lanes_on(proxy).move(BUILD_B)
    assert result.outcome == Outcome.JOB_FAILED
    assert result.error is not None
    assert "stopped before it had run 3 s" in result.error
    assert "site:b listening on :5678" in result.error


def test_a_new_instance_given_an_outside_network_during_its_check_is_not_switched_to() -> None:
    proxy = lane_brought_up_with_a()
    old = proxy.written[WEB_ADDRESS]
    proxy.while_checking = lambda container: proxy.host.attach(container, "bridge", quiet=False)
    with pytest.raises(Stopped, match="can reach out through bridge"):
        lanes_on(proxy).move(BUILD_B)
    assert proxy.written[WEB_ADDRESS] == old


def test_a_retry_after_a_crash_between_starting_the_job_and_watching_it_watches_it_before_answering() -> None:
    proxy = lane_brought_up_with_a()
    job = next(iter(proxy.host.jobs))
    proxy.host.jobs[job] = proxy.host.jobs[job].model_copy(update={"running_for_seconds": 0.5})
    result = lanes_on(proxy).move(BUILD_A)
    assert proxy.host.actions == [f"await job {job}"]
    assert result.outcome == Outcome.UNCHANGED
