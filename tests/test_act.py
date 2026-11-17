import threading
from collections.abc import Callable

import pytest

from launchplane.act import EVERY_LANE, MOST_STEPS, Lanes, Stopped
from launchplane.container_host import Asks, asks_no_database
from launchplane.docker_answers import ContainerHostError
from launchplane.instance import (
    BACK_SOON,
    InstanceState,
    LaneBuild,
    LoadedRoute,
    OnHost,
    RouteStatus,
    background_service_container_of,
)
from launchplane.judge import Work
from launchplane.product import Product, ProductError
from launchplane.steps import MaintenanceOutcome, Outcome, RetireOutcome
from tests.pretend_host import (
    SINCE,
    WEB_ADDRESS,
    WITH_DATA,
    LaunchplaneKilled,
    PretendHost,
    PretendProxy,
    lanes_on,
    numbered_names,
)

BUILD_A = LaneBuild(product="site", lane="testing", build="site:a")
BUILD_B = LaneBuild(product="site", lane="testing", build="site:b")


def lanes_serving(build: LaneBuild, container: str) -> tuple[Lanes, PretendProxy]:
    proxy = PretendProxy(PretendHost())
    proxy.host.place(build, container)
    proxy.route(WEB_ADDRESS, container)
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
    proxy = PretendProxy(PretendHost(), running=False)
    result = Lanes(proxy.host, proxy, name_fresh=numbered_names()).move(BUILD_B)
    assert proxy.host.actions[0] == "start proxy"
    assert result.outcome == Outcome.SWITCHED


def test_a_proxy_that_never_runs_stops_the_move_with_an_error_not_a_loop() -> None:
    proxy = PretendProxy(PretendHost(), running=False, starts=False)
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


def lane_brought_up_with_a() -> PretendProxy:
    proxy = PretendProxy(PretendHost())
    lanes_on(proxy).move(BUILD_A)
    proxy.host.actions.clear()
    return proxy


def test_a_lane_with_data_is_brought_up_data_set_first_and_background_service_last() -> None:
    proxy = PretendProxy(PretendHost())
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


def test_a_login_left_by_a_call_killed_after_its_container_went_is_removed_by_the_next_move_and_the_next_retire() -> None:
    proxy = lane_brought_up_with_a()
    proxy.host.actions.clear()
    proxy.host.change_database("database-data-1", unheld_logins=("i_left",))
    moved = lanes_on(proxy).move(BUILD_A)
    proxy.host.change_database("database-data-1", unheld_logins=("i_left_again",))
    retired = lanes_on(proxy).retire("site", "testing")
    assert (moved.outcome, proxy.host.actions[0], proxy.host.actions[-1]) == (
        Outcome.CLEANED_UP,
        "remove logins from database-data-1",
        "remove logins from database-data-1",
    )
    assert (retired.outcome, [each.unheld_logins for each in proxy.host.data_sets["data-1"].databases]) == (
        RetireOutcome.RETIRED,
        [()],
    )


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


def problems_by_lane(lanes: Lanes) -> dict[tuple[str, str], list[str]]:
    return {
        (judged.lane.product, judged.lane.lane): [finding.sentence for finding in judged.findings]
        for judged in lanes.read_status().lanes
    }


def at_work_by_lane(lanes: Lanes) -> dict[str, Work | None]:
    return {judged.lane.lane: judged.work for judged in lanes.read_status().lanes}


def test_every_lane_is_judged_for_the_status_call_even_one_whose_description_cannot_be_read() -> None:
    proxy = lane_brought_up_with_a()
    serving = proxy.written[WEB_ADDRESS]
    proxy.host.place(LaneBuild(product="broken", lane="testing", build="broken:a"), "broken-1")
    proxy.host.attach(serving, "bridge", quiet=False)

    def read_product(product: str) -> Product:
        if product == "broken":
            raise ProductError("The description of broken is not one Launchplane reads.")
        return WITH_DATA

    assert problems_by_lane(Lanes(proxy.host, proxy, read_product=read_product)) == {
        ("broken", "testing"): ["The description of broken is not one Launchplane reads.", "No instance receives visitors"],
        ("site", "testing"): [
            f"The instance {serving} is attached to bridge, which it must not be",
            f"The instance {serving} can reach out through bridge",
        ],
    }


def test_the_status_call_asks_every_lanes_database_and_a_call_only_its_own_lanes() -> None:
    proxy = lane_brought_up_with_a()
    lanes_on(proxy).move(LaneBuild(product="site", lane="other", build="site:a"))
    asked_by_a_move: list[set[str]] = []
    read_as_before = proxy.host.read

    def read_noting_who_is_asked(asks: Asks = asks_no_database) -> OnHost:
        asked_by_a_move.append({found.lane or "" for found in proxy.host.data_sets.values() if asks(found)})
        return read_as_before(asks)

    assert problems_by_lane(lanes_on(proxy)) == {("site", "other"): [], ("site", "testing"): []}
    proxy.host.read = read_noting_who_is_asked
    lanes_on(proxy).move(BUILD_A)
    assert asked_by_a_move == [{"testing"}]


def test_only_the_lane_being_moved_is_said_to_be_at_work_and_only_meanwhile() -> None:
    lanes, proxy = lanes_serving(BUILD_A, "a")
    proxy.host.place(LaneBuild(product="site", lane="other", build="site:a"), "other-1")
    at_work_during_the_check: list[dict[str, Work | None]] = []
    proxy.while_checking = lambda _container: at_work_during_the_check.append(at_work_by_lane(lanes))
    lanes.move(BUILD_B)
    assert at_work_during_the_check == [{"other": None, "testing": Work.MOVE}]
    assert at_work_by_lane(lanes) == {"other": None, "testing": None}


def test_a_move_that_stops_with_an_error_no_longer_counts_as_at_work_on_its_lane() -> None:
    lanes, proxy = lanes_serving(BUILD_A, "a")
    proxy.loaded[WEB_ADDRESS] = LoadedRoute(proxy.written[WEB_ADDRESS], RouteStatus.DISABLED)
    with pytest.raises(Stopped):
        lanes.move(BUILD_B)
    assert at_work_by_lane(lanes) == {"testing": None}


def test_a_move_that_ends_during_a_read_is_still_at_work_on_what_was_read() -> None:
    lanes, proxy = lanes_serving(BUILD_A, "a")
    read_as_before = proxy.host.read

    def read_while_the_move_ends(asks: Asks = asks_no_database) -> OnHost:
        on_host = read_as_before(asks)
        lanes.at_work = None
        return on_host

    lanes.at_work = (("site", "testing"), Work.MOVE)
    proxy.host.read = read_while_the_move_ends
    assert at_work_by_lane(lanes) == {"testing": Work.MOVE}


def web_addresses_read(proxy: PretendProxy) -> dict[str, tuple[str | None, bool, str | None]]:
    status = Lanes(proxy.host, proxy).read_status()
    return {entry.web_address: (entry.sends_to_container, entry.answers, entry.error) for entry in status.web_addresses}


def test_only_a_web_address_the_running_proxy_sends_to_a_product_is_asked_and_what_it_answered_is_in_the_reading() -> None:
    _, proxy = lanes_serving(BUILD_A, "a")
    proxy.host.place(LaneBuild(product="site", lane="routed-nowhere", build="site:a"), "nowhere-1")
    proxy.route("maintenance.site.test", BACK_SOON)
    proxy.not_answering[WEB_ADDRESS] = "502 Bad Gateway"
    answers = web_addresses_read(proxy)
    assert proxy.asked == [WEB_ADDRESS]
    assert answers[WEB_ADDRESS] == (
        "a",
        False,
        "The instance it is sent to is running, and does not answer with success: 502 Bad Gateway",
    )
    assert answers["maintenance.site.test"] == (BACK_SOON, False, None)
    assert answers["routed-nowhere.site.test"] == (None, False, "The proxy sends routed-nowhere.site.test to no instance.")
    proxy.running = False
    assert web_addresses_read(proxy)[WEB_ADDRESS] == (None, False, f"The proxy is not running, so {WEB_ADDRESS} answers nothing.")
    assert proxy.asked == [WEB_ADDRESS]


def test_web_addresses_are_asked_at_once_so_ones_that_hang_do_not_add_up() -> None:
    proxy = PretendProxy(PretendHost())
    for number in range(3):
        proxy.route(f"testing.site{number}.test", f"site{number}-testing")
    all_waiting = threading.Barrier(3, timeout=5)

    def hang_until_every_web_address_is_asked(web_address: str) -> str | None:
        assert web_address
        all_waiting.wait()
        return "download timed out"

    proxy.why_web_address_fails = hang_until_every_web_address_is_asked
    reading = Lanes(proxy.host, proxy).read(EVERY_LANE)
    assert set(reading.web_address_failures.values()) == {"download timed out"}


def state_of(proxy: PretendProxy) -> dict[str, object]:
    host = proxy.host
    return {
        "route": (proxy.loaded.get(WEB_ADDRESS), proxy.written.get(WEB_ADDRESS)),
        "instances": {name: instance.state for name, instance in host.instances.items()},
        "background services": {name: running.state for name, running in host.background_services.items()},
        "databases": {each.container: each.state for found in host.data_sets.values() for each in found.databases},
    }


def lane_in_maintenance() -> PretendProxy:
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


def test_ending_maintenance_removes_a_login_no_container_holds_before_the_lane_comes_back_so_asking_again_hides_nothing() -> None:
    proxy = lane_in_maintenance()
    proxy.host.logins_left_in["database-data-1"] = ("i_left_by_a_failed_start",)
    result = lanes_on(proxy).end_maintenance("site", "testing")
    again = lanes_on(proxy).end_maintenance("site", "testing")
    assert (result.outcome, again.outcome) == (MaintenanceOutcome.ENDED, MaintenanceOutcome.NOT_IN_MAINTENANCE)
    assert proxy.host.actions[-2:] == ["remove logins from database-data-1", f"route {proxy.written[WEB_ADDRESS]}"]
    assert [judged.findings for judged in lanes_on(proxy).read_status().lanes] == [()]


def test_ending_maintenance_on_a_lane_that_is_not_in_it_answers_from_the_mark_alone_with_nothing_else_readable() -> None:
    proxy = lane_brought_up_with_a()

    def unreadable(asks: Asks = asks_no_database) -> OnHost:
        raise ContainerHostError(f"Listing the containers on test failed: timed out, asked with {asks}")

    def no_description(product: str) -> Product:
        raise ProductError(f"The description of {product} is not one Launchplane reads.")

    proxy.host.read = unreadable
    result = Lanes(proxy.host, proxy, read_product=no_description).end_maintenance("site", "testing")
    assert (result.outcome, result.in_maintenance, proxy.host.actions) == (MaintenanceOutcome.NOT_IN_MAINTENANCE, False, [])


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


def test_a_web_part_that_will_not_start_coming_out_leaves_everything_stopped_and_says_why() -> None:
    proxy = lane_in_maintenance()
    stopped = state_of(proxy)
    proxy.host.parts_that_will_not_start.update(proxy.host.instances)
    result = lanes_on(proxy).end_maintenance("site", "testing")
    assert (result.outcome, result.in_maintenance) == (MaintenanceOutcome.WEB_PART_FAILED, True)
    assert "no such image" in (result.error or "")
    assert state_of(proxy) == stopped


def killed_then_asked_again(start: Callable[[], PretendProxy], call: Callable[[Lanes], object], after: int) -> dict[str, object]:
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
