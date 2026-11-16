import threading
from collections.abc import Callable
from datetime import UTC, datetime

import pytest

from launchplane.act import MOST_STEPS, Lanes, Stopped
from launchplane.container_host import HasDatabase, PartStopped
from launchplane.docker_answers import ContainerHostError, OnHost
from launchplane.instance import (
    BACK_SOON,
    LANE_NETWORK,
    DataSet,
    DataSetDatabase,
    DataSetUse,
    HostNetwork,
    InstanceState,
    LaneBuild,
    LaneRoute,
    LoadedRoute,
    Mount,
    ProxyNotRunning,
    RouteStatus,
    RunningBackgroundService,
    RunningInstance,
    background_service_container_of,
)
from launchplane.product import BackgroundService, Database, Product, ProductError
from launchplane.proxy import CheckFailed
from launchplane.steps import FreshNames, MaintenanceOutcome, Outcome, RetireOutcome

BUILD_A = LaneBuild(product="site", lane="testing", build="site:a")
BUILD_B = LaneBuild(product="site", lane="testing", build="site:b")
WEB_ADDRESS = "testing.site.test"
WITH_DATA = Product(
    health_path="/health",
    database=Database(image="postgres:18"),
    background_service=BackgroundService(command=["work"]),
)


class FakeHost:
    def __init__(self) -> None:
        self.instances: dict[str, RunningInstance] = {}
        self.background_services: dict[str, RunningBackgroundService] = {}
        self.data_sets: dict[str, DataSet] = {}
        self.networks: dict[str, bool] = {LANE_NETWORK: True}
        self.ready_databases: set[str] = set()
        self.actions: list[str] = []
        self.most_background_services_running = 0
        self.background_services_that_stop: set[str] = set()
        self.killed_after: int | None = None
        self.databases_that_must_be_killed: set[str] = set()
        self.parts_that_will_not_start: set[str] = set()

    def record(self, action: str) -> None:
        self.actions.append(action)
        if len(self.actions) == self.killed_after:
            raise LaunchplaneKilled

    def change_database(self, container: str, **change: object) -> None:
        for name, found in self.data_sets.items():
            databases = tuple(each.model_copy(update=change) if each.container == container else each for each in found.databases)
            self.data_sets[name] = found.model_copy(update={"databases": databases})

    def place(
        self, target: LaneBuild, container: str, state: InstanceState = InstanceState.RUNNING, use: DataSetUse | None = None
    ) -> None:
        self.instances[container] = RunningInstance(
            product=target.product,
            lane=target.lane,
            build=target.build,
            container=container,
            state=state,
            networks=(LANE_NETWORK,) if use is None else (LANE_NETWORK, use.network),
            data_set=None if use is None else use.data_set,
        )

    def attach(self, container: str, network: str, *, quiet: bool) -> None:
        self.networks[network] = quiet
        found = self.instances[container]
        self.instances[container] = found.model_copy(update={"networks": (*found.networks, network)})

    def read(self, has_database: HasDatabase | None = None) -> OnHost:
        networks = [HostNetwork(name=name, is_quiet=quiet) for name, quiet in self.networks.items()]
        data_sets = [
            self.asked(found) if has_database is not None and has_database(found.product) else found
            for found in self.data_sets.values()
        ]
        return OnHost(list(self.instances.values()), list(self.background_services.values()), data_sets, networks)

    def asked(self, found: DataSet) -> DataSet:
        databases = tuple(
            each.model_copy(update={"accepts_connections": each.container in self.ready_databases}) for each in found.databases
        )
        return found.model_copy(update={"databases": databases})

    def start_instance(self, target: LaneBuild, container: str, use: DataSetUse | None) -> None:
        self.place(target, container, use=use)
        self.record(f"start {container}")

    def start_background_service(self, target: LaneBuild, instance: str, use: DataSetUse | None, command: list[str]) -> None:
        container = background_service_container_of(instance)
        self.background_services[container] = RunningBackgroundService(
            product=target.product,
            lane=target.lane,
            build=target.build,
            container=container,
            state=InstanceState.RUNNING,
            networks=(LANE_NETWORK,) if use is None else (use.network,),
            running_for_seconds=0.0,
            instance=instance,
            data_set=None if use is None else use.data_set,
        )
        self.most_background_services_running = max(self.most_background_services_running, len(self.background_services))
        self.record(f"start background service {instance} on {None if use is None else use.network} with {' '.join(command)}")

    def await_background_service(self, container: str, steady_seconds: float) -> None:
        self.record(f"await background service {container}")
        if self.background_services[container].build in self.background_services_that_stop:
            self.background_services[container] = self.background_services[container].model_copy(
                update={"state": InstanceState.RESTARTING, "running_for_seconds": None}
            )
            raise PartStopped(
                f"The background service {container} is not running: it stopped before it had run {steady_seconds:g} s."
            )
        self.background_services[container] = self.background_services[container].model_copy(
            update={"running_for_seconds": steady_seconds}
        )

    def change_data_set(self, product: str, lane: str, data_set: str, **parts: object) -> None:
        found = self.data_sets.get(data_set) or DataSet(
            product=product, lane=lane, data_set=data_set, networks=(), volumes=(), databases=()
        )
        self.data_sets[data_set] = found.model_copy(update=parts)

    def create_data_set_network(self, product: str, lane: str, data_set: str) -> None:
        self.networks[f"network-{data_set}"] = True
        self.change_data_set(product, lane, data_set, networks=(HostNetwork(name=f"network-{data_set}", is_quiet=True),))
        self.record(f"network {data_set}")

    def create_data_set_volume(self, product: str, lane: str, data_set: str) -> None:
        self.change_data_set(product, lane, data_set, volumes=(f"volume-{data_set}",))
        self.record(f"volume {data_set}")

    def create_database(self, product: str, lane: str, data_set: str, network: str, volume: str, image: str) -> None:
        database = DataSetDatabase(
            container=f"database-{data_set}",
            state=InstanceState.RUNNING,
            networks=(network,),
            mounts=(Mount(source=volume, path="/var/lib/postgresql"),),
        )
        self.change_data_set(product, lane, data_set, databases=(database,))
        self.record(f"database {data_set} on {network} and {volume} from {image}")

    def start_database(self, container: str) -> None:
        self.change_database(container, state=InstanceState.RUNNING, exit_code=None)
        self.record(f"start database {container}")

    def stop_database(self, container: str) -> None:
        exit_code = 137 if container in self.databases_that_must_be_killed else 0
        self.change_database(container, state=InstanceState.STOPPED, exit_code=exit_code)
        self.ready_databases.discard(container)
        self.record(f"stop database {container}")

    def change_part(self, container: str, state: InstanceState) -> None:
        if container in self.background_services:
            ran = 0.0 if state == InstanceState.RUNNING else None
            self.background_services[container] = self.background_services[container].model_copy(
                update={"state": state, "running_for_seconds": ran}
            )
        else:
            self.instances[container] = self.instances[container].model_copy(update={"state": state})

    def start_part(self, container: str) -> None:
        if container in self.parts_that_will_not_start:
            raise ContainerHostError(f"Starting {container} on test failed: no such image")
        self.change_part(container, InstanceState.RUNNING)
        self.record(f"start {container}")

    def stop_part(self, container: str) -> None:
        self.change_part(container, InstanceState.STOPPED)
        self.record(f"stop {container}")

    def await_database(self, container: str) -> None:
        self.ready_databases.add(container)
        self.record(f"await {container}")

    def delete_data_set(self, found: DataSet) -> None:
        del self.data_sets[found.data_set]
        self.record(f"delete {found.data_set}")

    def remove_instance(self, container: str) -> None:
        del self.instances[container]
        self.record(f"remove {container}")

    def retire_instance(self, container: str) -> None:
        if container in self.background_services:
            del self.background_services[container]
        else:
            del self.instances[container]
        self.record(f"retire {container}")

    def read_log_tail(self, container: str) -> str:
        part = self.background_services.get(container) or self.instances[container]
        return f"{part.build} listening on :5678"


class LaunchplaneKilled(Exception):
    pass


SINCE = datetime(2026, 11, 16, 14, 2, tzinfo=UTC)


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
        return LaneRoute(
            None if loaded is None else LoadedRoute(loaded, RouteStatus.ENABLED),
            self.written.get(web_address),
            self.back_soon_since(web_address),
        )

    def back_soon_since(self, web_address: str) -> datetime | None:
        return SINCE if self.written.get(web_address) == BACK_SOON else None

    def show_back_soon(self, web_address: str) -> None:
        self.written[web_address] = BACK_SOON
        self.host.record("show back soon")

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


def test_an_instance_that_stops_while_its_route_is_awaited_is_replaced() -> None:
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


def test_a_lane_with_data_is_brought_up_data_set_first_and_background_service_last() -> None:
    proxy = FakeProxy(FakeHost())
    result = lanes_on(proxy).move(BUILD_A)
    new = proxy.written[WEB_ADDRESS]
    assert (result.outcome, result.data_set) == (Outcome.SWITCHED, "data-1")
    assert proxy.host.actions == [
        "network data-1",
        "volume data-1",
        "database data-1 on network-data-1 and volume-data-1 from postgres:18",
        "await database-data-1",
        f"start {new}",
        f"check {new}/health",
        f"route {new}",
        f"start background service {new} on network-data-1 with work",
        f"await background service {background_service_container_of(new)}",
    ]


def test_a_release_keeps_the_data_set_and_stops_the_old_background_service_before_the_new_one_starts() -> None:
    proxy = lane_brought_up_with_a()
    old = proxy.written[WEB_ADDRESS]
    result = lanes_on(proxy).move(BUILD_B)
    new = proxy.written[WEB_ADDRESS]
    assert proxy.host.actions == [
        f"start {new}",
        f"check {new}/health",
        f"route {new}",
        f"retire {background_service_container_of(old)}",
        f"start background service {new} on network-data-1 with work",
        f"await background service {background_service_container_of(new)}",
        f"retire {old}",
    ]
    assert result.data_set == "data-1"
    assert proxy.host.most_background_services_running == 1


def test_retiring_a_lane_with_data_stops_its_background_service_first_and_keeps_the_data_set_unless_named() -> None:
    proxy = lane_brought_up_with_a()
    instance = proxy.written[WEB_ADDRESS]
    kept = lanes_on(proxy).retire("site", "testing")
    assert proxy.host.actions == ["unroute", f"retire {background_service_container_of(instance)}", f"retire {instance}"]
    assert (kept.deleted_data_set, kept.kept_data_sets) == (None, ["data-1"])
    deleted = lanes_on(proxy).retire("site", "testing", delete_data_set="data-1")
    assert (deleted.outcome, deleted.deleted_data_set, deleted.kept_data_sets) == (RetireOutcome.DELETED, "data-1", [])


def test_naming_a_data_set_of_another_lane_deletes_nothing() -> None:
    proxy = lane_brought_up_with_a()
    result = lanes_on(proxy).retire("site", "other", delete_data_set="data-1")
    assert (result.outcome, result.deleted_data_set) == (RetireOutcome.UNCHANGED, None)
    assert list(proxy.host.data_sets) == ["data-1"]


def test_a_retry_after_a_crash_before_the_background_service_was_watched_watches_it() -> None:
    proxy = lane_brought_up_with_a()
    background_service = next(iter(proxy.host.background_services))
    proxy.host.background_services[background_service] = proxy.host.background_services[background_service].model_copy(
        update={"running_for_seconds": 0.5}
    )
    result = lanes_on(proxy).move(BUILD_A)
    assert proxy.host.actions == [f"await background service {background_service}"]
    assert result.outcome == Outcome.UNCHANGED


def test_a_retire_asked_during_a_move_waits_for_it() -> None:
    lanes, proxy = lanes_serving(BUILD_A, "a")
    checking, checked = threading.Event(), threading.Event()

    def hold_the_check(_container: str) -> None:
        checking.set()
        assert checked.wait(timeout=5)

    proxy.while_checking = hold_the_check
    move = threading.Thread(target=lanes.move, args=(BUILD_B,))
    retire = threading.Thread(target=lanes.retire, args=("site", "testing"))
    move.start()
    assert checking.wait(timeout=5)
    retire.start()
    retire.join(timeout=0.3)
    done_while_the_move_was_held = list(proxy.host.actions)
    checked.set()
    move.join(timeout=5)
    retire.join(timeout=5)
    assert [action.split()[0] for action in done_while_the_move_was_held] == ["start", "check"]
    assert proxy.host.actions[-2:] == ["unroute", f"retire {proxy.host.actions[0].split()[1]}"]


def test_every_lane_is_judged_for_the_page_even_one_whose_description_cannot_be_read() -> None:
    proxy = lane_brought_up_with_a()
    serving = proxy.written[WEB_ADDRESS]
    proxy.host.place(LaneBuild(product="broken", lane="testing", build="broken:a"), "broken-1")
    proxy.host.attach(serving, "bridge", quiet=False)

    def read_product(product: str) -> Product:
        if product == "broken":
            raise ProductError("The description of broken is not one Launchplane reads.")
        return WITH_DATA

    judged = Lanes(proxy.host, proxy, read_product=read_product).read_judged({WEB_ADDRESS: serving})
    verdicts = {(verdict.product, verdict.lane): verdict.problems for verdict in judged.lanes}
    assert verdicts[("broken", "testing")] == ["The description of broken is not one Launchplane reads."]
    assert verdicts[("site", "testing")] == [
        f"The instance {serving} is attached to bridge, which it must not be",
        f"The instance {serving} can reach out through bridge",
    ]


def test_a_lane_as_it_must_be_is_judged_for_the_page_with_its_database_asked() -> None:
    proxy = lane_brought_up_with_a()
    judged = lanes_on(proxy).read_judged({WEB_ADDRESS: proxy.written[WEB_ADDRESS]})
    assert [verdict.problems for verdict in judged.lanes] == [[]]


def test_only_the_lane_being_moved_is_said_to_be_moving_and_only_meanwhile() -> None:
    lanes, proxy = lanes_serving(BUILD_A, "a")
    proxy.host.place(LaneBuild(product="site", lane="other", build="site:a"), "other-1")
    moving_during_the_check: list[dict[str, bool]] = []

    def judge_now(_container: str) -> None:
        judged = lanes.read_judged({WEB_ADDRESS: "a"})
        moving_during_the_check.append({verdict.lane: verdict.a_move_is_at_work for verdict in judged.lanes})

    proxy.while_checking = judge_now
    lanes.move(BUILD_B)
    after = {verdict.lane: verdict.a_move_is_at_work for verdict in lanes.read_judged(dict(proxy.written)).lanes}
    assert moving_during_the_check == [{"other": False, "testing": True}]
    assert after == {"other": False, "testing": False}


def test_a_move_that_stops_with_an_error_no_longer_counts_as_at_work_on_its_lane() -> None:
    lanes, proxy = lanes_serving(BUILD_A, "a")
    proxy.read_lane_route = lambda web_address: LaneRoute(LoadedRoute("a", RouteStatus.DISABLED), "a")
    with pytest.raises(Stopped):
        lanes.move(BUILD_B)
    assert [verdict.a_move_is_at_work for verdict in lanes.read_judged({WEB_ADDRESS: "a"}).lanes] == [False]


def test_a_move_that_ends_during_a_read_is_still_at_work_on_what_was_read() -> None:
    lanes, proxy = lanes_serving(BUILD_A, "a")
    read_as_before = proxy.host.read

    def read_while_the_move_ends(has_database: HasDatabase | None = None) -> OnHost:
        on_host = read_as_before(has_database)
        lanes.lane_being_moved = None
        return on_host

    lanes.lane_being_moved = ("site", "testing")
    proxy.host.read = read_while_the_move_ends
    assert [verdict.a_move_is_at_work for verdict in lanes.read_judged({WEB_ADDRESS: "a"}).lanes] == [True]


def state_of(proxy: FakeProxy) -> dict[str, object]:
    host = proxy.host
    return {
        "route": (proxy.loaded.get(WEB_ADDRESS), proxy.written.get(WEB_ADDRESS)),
        "instances": {name: instance.state for name, instance in host.instances.items()},
        "background services": {name: running.state for name, running in host.background_services.items()},
        "databases": {each.container: each.state for found in host.data_sets.values() for each in found.databases},
    }


def lane_in_maintenance() -> FakeProxy:
    proxy = lane_brought_up_with_a()
    lanes_on(proxy).begin_maintenance("site", "testing")
    proxy.host.actions.clear()
    return proxy


def test_a_lane_goes_into_maintenance_route_first_then_background_service_web_part_and_database_last() -> None:
    proxy = lane_brought_up_with_a()
    web = proxy.written[WEB_ADDRESS]
    result = lanes_on(proxy).begin_maintenance("site", "testing")
    assert proxy.host.actions == [
        "show back soon",
        f"stop {background_service_container_of(web)}",
        f"stop {web}",
        "stop database database-data-1",
    ]
    assert (result.outcome, result.in_maintenance, result.since) == (MaintenanceOutcome.IN_MAINTENANCE, True, SINCE)
    assert set(proxy.host.instances) == {web}
    assert list(proxy.host.data_sets) == ["data-1"]


def test_beginning_maintenance_again_changes_nothing_and_answers_the_same() -> None:
    proxy = lane_brought_up_with_a()
    first = lanes_on(proxy).begin_maintenance("site", "testing")
    proxy.host.actions.clear()
    assert lanes_on(proxy).begin_maintenance("site", "testing") == first
    assert proxy.host.actions == []


def test_a_database_that_had_to_be_killed_is_answered_so_every_time_and_never_as_in_maintenance_cleanly() -> None:
    proxy = lane_brought_up_with_a()
    proxy.host.databases_that_must_be_killed.add("database-data-1")
    answers = [lanes_on(proxy).begin_maintenance("site", "testing") for _ in range(2)]
    assert [answer.outcome for answer in answers] == [MaintenanceOutcome.DATABASE_KILLED] * 2
    assert "exited with code 137" in (answers[0].error or "")


def test_a_lane_comes_out_of_maintenance_database_first_and_its_web_address_switches_back_last() -> None:
    proxy = lane_in_maintenance()
    [web] = proxy.host.instances
    result = lanes_on(proxy).end_maintenance("site", "testing")
    assert proxy.host.actions == [
        "start database database-data-1",
        "await database-data-1",
        f"start {web}",
        f"check {web}/health",
        f"start {background_service_container_of(web)}",
        f"await background service {background_service_container_of(web)}",
        f"route {web}",
    ]
    assert (result.outcome, result.in_maintenance) == (MaintenanceOutcome.ENDED, False)
    assert lanes_on(proxy).move(BUILD_A).outcome == Outcome.UNCHANGED


def test_ending_maintenance_on_a_lane_that_is_not_in_it_changes_nothing_and_says_so() -> None:
    proxy = lane_brought_up_with_a()
    result = lanes_on(proxy).end_maintenance("site", "testing")
    assert (result.outcome, proxy.host.actions) == (MaintenanceOutcome.NOT_IN_MAINTENANCE, [])


def test_a_web_part_that_fails_its_check_coming_out_leaves_everything_stopped_behind_the_back_soon_page() -> None:
    proxy = lane_in_maintenance()
    stopped = state_of(proxy)
    [web] = proxy.host.instances
    proxy.failing.add(web)
    result = lanes_on(proxy).end_maintenance("site", "testing")
    assert (result.outcome, result.in_maintenance) == (MaintenanceOutcome.WEB_PART_FAILED, True)
    assert "Connection refused" in (result.error or "")
    assert state_of(proxy) == stopped
    assert proxy.host.actions[-2:] == [f"stop {web}", "stop database database-data-1"]


def test_a_failed_check_whose_log_cannot_be_read_still_leaves_everything_stopped_and_is_answered() -> None:
    proxy = lane_in_maintenance()
    stopped = state_of(proxy)
    [web] = proxy.host.instances
    proxy.failing.add(web)

    def unreadable(container: str) -> str:
        raise ContainerHostError(f"Reading the log of {container} on test failed: timed out")

    proxy.host.read_log_tail = unreadable
    result = lanes_on(proxy).end_maintenance("site", "testing")
    assert (result.outcome, result.in_maintenance) == (MaintenanceOutcome.WEB_PART_FAILED, True)
    assert "Connection refused Its log could not be read" in (result.error or "")
    assert state_of(proxy) == stopped


def test_a_background_service_that_does_not_stay_up_coming_out_leaves_everything_stopped_behind_the_back_soon_page() -> None:
    proxy = lane_in_maintenance()
    stopped = state_of(proxy)
    proxy.host.background_services_that_stop.add(BUILD_A.build)
    result = lanes_on(proxy).end_maintenance("site", "testing")
    assert (result.outcome, result.in_maintenance) == (MaintenanceOutcome.BACKGROUND_SERVICE_FAILED, True)
    assert state_of(proxy) == stopped


def test_ending_maintenance_starts_no_stopped_background_service_on_a_lane_that_is_not_in_maintenance() -> None:
    proxy = lane_brought_up_with_a()
    [background_service] = proxy.host.background_services
    proxy.host.stop_part(background_service)
    proxy.host.actions.clear()
    result = lanes_on(proxy).end_maintenance("site", "testing")
    assert (result.outcome, proxy.host.actions) == (MaintenanceOutcome.NOT_IN_MAINTENANCE, [])


def test_a_web_part_that_will_not_start_coming_out_leaves_everything_stopped_and_says_why() -> None:
    proxy = lane_in_maintenance()
    stopped = state_of(proxy)
    proxy.host.parts_that_will_not_start.update(proxy.host.instances)
    result = lanes_on(proxy).end_maintenance("site", "testing")
    assert (result.outcome, result.in_maintenance) == (MaintenanceOutcome.WEB_PART_FAILED, True)
    assert "no such image" in (result.error or "")
    assert state_of(proxy) == stopped


def test_a_move_on_a_lane_in_maintenance_refuses_and_starts_nothing() -> None:
    proxy = lane_in_maintenance()
    with pytest.raises(Stopped, match="End maintenance first"):
        lanes_on(proxy).move(BUILD_B)
    assert proxy.host.actions == []


def test_a_lane_in_maintenance_is_retired_as_any_other_and_its_data_set_is_kept() -> None:
    proxy = lane_in_maintenance()
    [web] = proxy.host.instances
    result = lanes_on(proxy).retire("site", "testing")
    assert proxy.host.actions == ["unroute", f"retire {background_service_container_of(web)}", f"retire {web}"]
    assert (result.outcome, result.kept_data_sets) == (RetireOutcome.RETIRED, ["data-1"])


def killed_then_asked_again(start: Callable[[], FakeProxy], call: Callable[[Lanes], object], after: int) -> dict[str, object]:
    proxy = start()
    proxy.host.killed_after = len(proxy.host.actions) + after
    with pytest.raises(LaunchplaneKilled):
        call(lanes_on(proxy))
    proxy.host.killed_after = None
    call(lanes_on(proxy))
    return state_of(proxy)


@pytest.mark.parametrize("after", range(1, 5))
def test_beginning_maintenance_killed_after_any_action_is_finished_by_the_same_call(after: int) -> None:
    def begin(lanes: Lanes) -> object:
        return lanes.begin_maintenance("site", "testing")

    assert killed_then_asked_again(lane_brought_up_with_a, begin, after) == state_of(lane_in_maintenance())


@pytest.mark.parametrize("after", range(1, 8))
def test_ending_maintenance_killed_after_any_action_is_finished_by_the_same_call(after: int) -> None:
    def end(lanes: Lanes) -> object:
        return lanes.end_maintenance("site", "testing")

    brought_back = lane_in_maintenance()
    end(lanes_on(brought_back))
    assert killed_then_asked_again(lane_in_maintenance, end, after) == state_of(brought_back)


def test_a_lane_in_maintenance_is_judged_for_the_page_with_since_when() -> None:
    proxy = lane_in_maintenance()
    [verdict] = lanes_on(proxy).read_judged({WEB_ADDRESS: BACK_SOON}).lanes
    assert (verdict.in_maintenance, verdict.in_maintenance_since, verdict.problems) == (True, SINCE, [])
