import threading
import time
from collections.abc import Callable
from typing import Any, NoReturn

import pytest
from fastapi.testclient import TestClient

from launchplane.act import Lanes
from launchplane.api import create_app
from launchplane.docker_answers import ContainerHostError, ContainerHostRefused
from launchplane.gate import Answer as GateAnswer
from launchplane.gate import GateError
from launchplane.instance import BACK_SOON, DataSetUse, InstanceState, LaneBuild, LoadedRoute, OnHost, RouteStatus, Verdict
from launchplane.judge import Work
from launchplane.page import request_note
from launchplane.record import MOST_ATTEMPTS, Asked, Record, RecordError, Refused, Request, Stands
from launchplane.worker import Worker
from tests.given_secrets import NO_SECRETS, GivenSecrets
from tests.pretend_host import (
    EARLIER,
    WEB_ADDRESS,
    WITH_DATA,
    GateNeverAsked,
    LaunchplaneKilled,
    PretendGate,
    PretendHost,
    PretendProxy,
    numbered_names,
)
from tests.pretend_record import PretendRecord
from tests.test_act import lane_brought_up_with_a, lane_in_maintenance, lanes_on
from tests.test_record import taken_up_until_it_needs_a_person
from tests.test_requests import MOVE, TO_B, Bed, bed_on
from tests.test_worker import CALLER, MOVE_B, delete_every_event, kept_of, killed_while_checking, serving_a, stopped, wait_until

BY_HAND = "a call from the address 192.0.2.7"
MOVE_C = Asked(product="site", lane="testing", work=Work.MOVE, build="site:c")
RETIRE = Asked(product="site", lane="testing", work=Work.RETIRE)
BEGIN = Asked(product="site", lane="testing", work=Work.BEGIN_MAINTENANCE)
END = Asked(product="site", lane="testing", work=Work.END_MAINTENANCE)
TO_BUILD_B = LaneBuild(product="site", lane="testing", build="site:b")
NEW = "site-testing-0"
NO_VERDICT = GateAnswer(Verdict.NONE)


# What a test gives the pretend proxy to call at the moment the cancel is to arrive.
def cancelling(record: Record, request: int) -> Callable[..., None]:
    def cancel(*_at: object) -> None:
        record.cancel(request, BY_HAND)

    return cancel


def put_back_by(request: Request) -> list[str]:
    assert request.answer is not None
    return request.answer["put_back"]


# As a request stands when Launchplane has stopped just after taking it up: working, with nothing done.
def taken_up_and_left(record: Record, asked: Asked) -> int:
    request = record.write(asked, CALLER, None)
    assert record.take_up(request.id) is not None
    return request.id


def test_a_request_that_waits_ends_as_cancelled_with_nothing_done(record: Record) -> None:
    worker, _, proxy = serving_a(record)
    request = record.write(MOVE_B, CALLER, None)
    cancelled = record.cancel(request.id, BY_HAND)
    assert cancelled is not None
    assert (cancelled.stands, put_back_by(cancelled), cancelled.cancel_asked_by) == (Stands.CANCELLED, [], BY_HAND)
    assert (worker.work_next(), proxy.host.actions, kept_of(record, request.id).attempts) == (False, [], [])


def test_a_move_cancelled_before_the_switch_removes_the_instance_it_started_and_visitors_stay_where_they_were(
    record: Record,
) -> None:
    worker, _, proxy = serving_a(record)
    request = record.write(MOVE_B, CALLER, None)
    proxy.while_checking = cancelling(record, request.id)
    assert worker.work_next() is True
    kept = kept_of(record, request.id)
    assert (kept.stands, put_back_by(kept), kept.cancel_asked_by, kept.error) == (
        Stands.CANCELLED,
        [f"retire {NEW}"],
        BY_HAND,
        None,
    )
    assert (proxy.written, list(proxy.host.instances)) == ({WEB_ADDRESS: "a"}, ["a"])
    assert ([each.ended for each in kept.attempts], kept.events[-1].did) == (["cancelled"], f"retire {NEW}, to put the lane back")


def test_a_move_cancelled_after_the_switch_is_finished_as_asked_and_says_the_cancel_came_too_late(record: Record) -> None:
    worker, _, proxy = serving_a(record)
    request = record.write(MOVE_B, CALLER, None)
    proxy.while_awaiting = cancelling(record, request.id)
    assert worker.work_next() is True
    kept = kept_of(record, request.id)
    assert kept.answer is not None and kept.cancel_refused is not None
    assert (kept.stands, kept.answer["outcome"], proxy.written, list(proxy.host.instances)) == (
        Stands.DONE,
        "switched",
        {WEB_ADDRESS: NEW},
        [NEW],
    )
    assert "already renamed for this release" in kept.cancel_refused and "finished as asked" in kept.cancel_refused


@pytest.mark.parametrize("events_deleted", [False, True], ids=["with its events", "with every event deleted"])
def test_after_a_restart_a_request_that_was_being_cancelled_is_put_back_from_what_the_host_shows_whatever_the_events_say(
    record: Record, events_deleted: bool
) -> None:
    worker, lanes, proxy = serving_a(record)
    request = killed_while_checking(record, lanes, proxy)
    record.cancel(request, BY_HAND)
    if events_deleted:
        delete_every_event(record, request)
    assert worker.work_next() is True
    kept = kept_of(record, request)
    assert (kept.stands, [each.ended for each in kept.attempts]) == (Stands.CANCELLED, ["interrupted", "cancelled"])
    assert (proxy.host.actions, put_back_by(kept), proxy.written) == (
        [f"retire {NEW}"],
        [f"retire {NEW}"],
        {WEB_ADDRESS: "a"},
    )


def test_a_move_killed_just_after_the_switch_is_too_late_to_cancel_though_no_event_says_it_switched(record: Record) -> None:
    worker, lanes, proxy = serving_a(record)
    request = taken_up_and_left(record, MOVE_B)
    proxy.host.killed_after = len(["start", "check", "route"])
    with pytest.raises(LaunchplaneKilled):
        lanes.move(TO_BUILD_B, kept_of(record, request).generation, 1)
    proxy.host.killed_after = None
    record.cancel(request, BY_HAND)
    assert worker.work_next() is True
    kept = kept_of(record, request)
    assert kept.cancel_refused is not None and "already renamed for this release" in kept.cancel_refused
    assert (kept.stands, kept.answer and kept.answer["outcome"], list(proxy.host.instances)) == (Stands.DONE, "switched", [NEW])


def test_a_newer_request_cancels_the_one_being_worked_and_the_lane_ends_on_the_newer_build_with_visitors_never_on_the_older(
    record: Record,
) -> None:
    worker, _, proxy = serving_a(record)
    older = record.write(MOVE_B, CALLER, None)
    newer: list[int] = []

    def ask_for_c_while_b_is_checked(_container: str) -> None:
        if not newer:
            newer.append(record.write(MOVE_C, CALLER, None).id)

    proxy.while_checking = ask_for_c_while_b_is_checked
    assert (worker.work_next(), worker.work_next(), worker.work_next()) == (True, True, False)
    cancelled, done = kept_of(record, older.id), kept_of(record, newer[0])
    routed = [action for action in proxy.host.actions if action.startswith("route")]
    assert (cancelled.stands, cancelled.cancel_asked_by, done.stands) == (Stands.CANCELLED, f"request {done.id}", Stands.DONE)
    assert (routed, [each.build for each in proxy.host.instances.values()]) == ([f"route {proxy.written[WEB_ADDRESS]}"], ["site:c"])


def test_a_newer_request_ends_one_that_waits_as_cancelled_and_only_the_newer_is_worked(record: Record) -> None:
    worker, _, proxy = serving_a(record)
    older, newer = record.write(MOVE_B, CALLER, None), record.write(MOVE_C, CALLER, None)
    assert (worker.work_next(), worker.work_next()) == (True, False)
    cancelled = kept_of(record, older.id)
    assert (cancelled.stands, cancelled.cancel_asked_by, cancelled.attempts) == (Stands.CANCELLED, f"request {newer.id}", [])
    assert [each.build for each in proxy.host.instances.values()] == ["site:c"]


def test_asking_again_for_exactly_what_an_unfinished_request_asks_cancels_nothing(record: Record) -> None:
    first = record.write(MOVE_B, CALLER, None)
    again = record.write(MOVE_B, CALLER, None)
    kept = kept_of(record, first.id)
    assert (again.id, kept.stands, kept.cancel_asked_by) == (first.id, Stands.WAITING, None)


def test_asking_for_what_a_request_being_cancelled_asks_writes_a_new_request_and_does_not_answer_with_that_one(
    record: Record,
) -> None:
    being_cancelled = taken_up_and_left(record, MOVE_B)
    record.cancel(being_cancelled, BY_HAND)
    assert record.write(MOVE_B, CALLER, None).id != being_cancelled


def test_a_request_that_needs_a_person_is_cancelled_neither_by_a_newer_request_nor_by_the_call(record: Record) -> None:
    stuck = record.write(MOVE_B, CALLER, None)
    taken_up_until_it_needs_a_person(record, stuck.id)
    with pytest.raises(Refused, match="needs a person"):
        record.write(MOVE_C, CALLER, None)
    after = record.cancel(stuck.id, BY_HAND)
    assert after is not None and (after.stands, after.cancel_asked_by) == (Stands.NEEDS_A_PERSON, None)


def test_cancelling_a_request_that_has_ended_changes_nothing_of_it(record: Record) -> None:
    request = record.write(MOVE_B, CALLER, None)
    attempt = record.take_up(request.id)
    assert attempt is not None
    record.end(attempt, Stands.DONE, {"outcome": "switched"}, None)
    before = kept_of(record, request.id)
    record.cancel(request.id, BY_HAND)
    assert (kept_of(record, request.id), record.cancel(999, BY_HAND)) == (before, None)


def test_a_retire_cancelled_before_its_route_is_removed_changes_nothing(record: Record) -> None:
    worker, _, proxy = serving_a(record)
    request = taken_up_and_left(record, RETIRE)
    record.cancel(request, BY_HAND)
    assert worker.work_next() is True
    kept = kept_of(record, request)
    assert (kept.stands, put_back_by(kept), proxy.host.actions, proxy.written) == (
        Stands.CANCELLED,
        [],
        [],
        {WEB_ADDRESS: "a"},
    )


def test_a_retire_cancelled_once_its_route_is_gone_is_finished_as_asked_and_says_it_cannot_be_put_back(record: Record) -> None:
    worker, _, proxy = serving_a(record)
    request = record.write(RETIRE, CALLER, None)
    proxy.while_awaiting = cancelling(record, request.id)
    assert worker.work_next() is True
    kept = kept_of(record, request.id)
    assert kept.cancel_refused is not None and "has no route" in kept.cancel_refused
    assert (kept.stands, proxy.host.actions, proxy.host.instances) == (Stands.DONE, ["unroute", "retire a"], {})


def test_beginning_maintenance_cancelled_before_the_back_soon_page_shows_changes_nothing(record: Record) -> None:
    worker, _, proxy = serving_a(record)
    request = taken_up_and_left(record, BEGIN)
    record.cancel(request, BY_HAND)
    assert worker.work_next() is True
    assert (kept_of(record, request).stands, proxy.host.actions, proxy.written) == (Stands.CANCELLED, [], {WEB_ADDRESS: "a"})


def test_beginning_maintenance_cancelled_once_the_back_soon_page_shows_is_finished_as_asked_and_says_how_to_come_back(
    record: Record,
) -> None:
    worker, _, proxy = serving_a(record)
    request = record.write(BEGIN, CALLER, None)
    proxy.while_awaiting = cancelling(record, request.id)
    assert worker.work_next() is True
    kept = kept_of(record, request.id)
    assert kept.cancel_refused is not None and "/lanes/site/testing/maintenance/end" in kept.cancel_refused
    assert (kept.stands, proxy.host.actions, proxy.written) == (Stands.DONE, ["show back soon", "stop a"], {WEB_ADDRESS: BACK_SOON})


def test_ending_maintenance_cancelled_before_the_switch_back_stops_again_what_it_started(record: Record) -> None:
    worker, lanes, proxy = serving_a(record)
    lanes.begin_maintenance("site", "testing")
    proxy.host.actions.clear()
    request = record.write(END, CALLER, None)
    proxy.while_checking = cancelling(record, request.id)
    assert worker.work_next() is True
    kept = kept_of(record, request.id)
    assert (kept.stands, put_back_by(kept), proxy.host.actions) == (
        Stands.CANCELLED,
        ["stop a"],
        ["start a", "check a/", "stop a"],
    )
    assert (proxy.written, proxy.host.instances["a"].state) == ({WEB_ADDRESS: BACK_SOON}, InstanceState.STOPPED)


def test_ending_maintenance_cancelled_after_the_switch_back_is_finished_as_asked(record: Record) -> None:
    worker, lanes, proxy = serving_a(record)
    lanes.begin_maintenance("site", "testing")
    request = record.write(END, CALLER, None)
    proxy.while_awaiting = cancelling(record, request.id)
    assert worker.work_next() is True
    kept = kept_of(record, request.id)
    assert kept.cancel_refused is not None and "already sends its visitors to a" in kept.cancel_refused
    assert (kept.stands, proxy.written) == (Stands.DONE, {WEB_ADDRESS: "a"})


def test_a_move_in_a_lane_in_maintenance_that_is_cancelled_leaves_the_lanes_stopped_instance_of_that_build(record: Record) -> None:
    worker, lanes, proxy = serving_a(record)
    lanes.begin_maintenance("site", "testing")
    request = taken_up_and_left(record, Asked(product="site", lane="testing", work=Work.MOVE, build="site:a"))
    record.cancel(request, BY_HAND)
    assert worker.work_next() is True
    assert (kept_of(record, request).stands, list(proxy.host.instances)) == (Stands.CANCELLED, ["a"])


def test_a_cancelled_first_move_keeps_the_data_set_it_made_names_it_and_is_still_a_clean_cancel(record: Record) -> None:
    proxy = PretendProxy(PretendHost())
    lanes = Lanes(proxy.host, proxy, read_product=lambda _product: WITH_DATA, name_fresh=numbered_names())
    request = record.write(MOVE_B, CALLER, None)
    proxy.while_checking = cancelling(record, request.id)
    assert Worker(record, lanes, NO_SECRETS).work_next() is True
    kept = kept_of(record, request.id)
    deleted = [action for action in proxy.host.actions if action.startswith("delete")]
    assert kept.answer is not None
    assert (kept.stands, proxy.host.instances, deleted) == (Stands.CANCELLED, {}, [])
    assert (kept.answer["data_sets_kept"], "No instance receives visitors" in kept.answer["findings"]) == (
        list(proxy.host.data_sets),
        True,
    )


def test_cancelling_never_removes_the_instance_the_proxy_still_sends_visitors_to_though_the_routes_file_names_another(
    record: Record,
) -> None:
    worker, _, proxy = serving_a(record)
    proxy.host.place(TO_BUILD_B, "b")
    proxy.written[WEB_ADDRESS] = "b"
    request = taken_up_and_left(record, Asked(product="site", lane="testing", work=Work.MOVE, build="site:a"))
    record.cancel(request, BY_HAND)
    assert worker.work_next() is True
    kept = kept_of(record, request)
    assert kept.answer is not None and kept.answer["findings"] == [
        f"The proxy has not loaded the routes file of {WEB_ADDRESS}: the file sends it to b and the proxy to a",
        "The instance b runs beside the active one",
    ]
    assert (kept.stands, sorted(proxy.host.instances), proxy.loaded[WEB_ADDRESS]) == (
        Stands.CANCELLED,
        ["a", "b"],
        LoadedRoute("a", RouteStatus.ENABLED),
    )


def test_cancelling_a_move_leaves_an_instance_of_its_build_that_an_earlier_request_started(record: Record) -> None:
    worker, lanes, proxy = serving_a(record)
    request = killed_while_checking(record, lanes, proxy)
    proxy.host.instances[NEW] = proxy.host.instances[NEW].model_copy(update={"generation": EARLIER})
    record.cancel(request, BY_HAND)
    assert worker.work_next() is True
    assert (kept_of(record, request).stands, sorted(proxy.host.instances), proxy.host.actions) == (Stands.CANCELLED, ["a", NEW], [])


def test_a_move_to_the_serving_build_cancelled_before_it_changed_anything_is_not_too_late_and_changes_nothing(
    record: Record,
) -> None:
    proxy = lane_brought_up_with_a()
    [missing] = proxy.host.background_services
    del proxy.host.background_services[missing]
    proxy.host.actions.clear()
    request = taken_up_and_left(record, Asked(product="site", lane="testing", work=Work.MOVE, build="site:a"))
    record.cancel(request, BY_HAND)
    assert Worker(record, lanes_on(proxy), NO_SECRETS).work_next() is True
    kept = kept_of(record, request)
    assert (kept.stands, kept.cancel_refused, proxy.host.actions, proxy.host.background_services) == (Stands.CANCELLED, None, [], {})


def test_a_background_service_the_cancelled_request_started_is_retired_and_the_instance_it_did_not_make_stays(
    record: Record,
) -> None:
    proxy = lane_brought_up_with_a()
    [missing] = proxy.host.background_services
    del proxy.host.background_services[missing]
    proxy.host.actions.clear()
    request = record.write(Asked(product="site", lane="testing", work=Work.MOVE, build="site:a"), CALLER, None)
    await_background_service = proxy.host.await_background_service

    def cancel_while_it_is_watched(container: str, steady_seconds: float) -> None:
        record.cancel(request.id, BY_HAND)
        await_background_service(container, steady_seconds)

    proxy.host.await_background_service = cancel_while_it_is_watched
    assert Worker(record, lanes_on(proxy), NO_SECRETS).work_next() is True
    kept = kept_of(record, request.id)
    assert (kept.stands, put_back_by(kept), proxy.host.background_services, len(proxy.host.instances)) == (
        Stands.CANCELLED,
        [f"retire {missing}"],
        {},
        1,
    )


def test_a_release_whose_new_background_service_does_not_stay_up_stays_working_and_says_why_since_it_is_switched(
    record: Record,
) -> None:
    proxy = lane_brought_up_with_a()
    proxy.host.background_services_that_stop.add("site:b")
    start = proxy.host.start_background_service

    def start_one_that_stops_at_once(
        target: LaneBuild, instance: str, use: DataSetUse | None, command: list[str], generation: str | None = None
    ) -> None:
        start(target, instance, use, command, generation)
        proxy.host.change_part(f"{instance}-background-service", InstanceState.RESTARTING)

    proxy.host.start_background_service = start_one_that_stops_at_once
    asked = record.write(MOVE_B, CALLER, None)
    assert Worker(record, lanes_on(proxy), NO_SECRETS).work_next() is True
    kept = kept_of(record, asked.id)
    started = [action for action in proxy.host.actions if action.startswith("start background service")]
    assert kept.error is not None and "The background service did not stay up" in kept.error
    assert (kept.stands, kept.answer, len(started), proxy.written[WEB_ADDRESS] != "a") == (Stands.WORKING, None, 1, True)


def test_a_cancel_that_killed_a_database_while_stopping_it_again_still_fails_when_another_attempt_ends_it(
    record: Record,
) -> None:
    proxy = lane_in_maintenance()
    lanes = lanes_on(proxy)
    proxy.host.databases_that_must_be_killed.add("database-data-1")
    request = record.write(END, CALLER, None)
    proxy.while_checking = cancelling(record, request.id)
    end = record.end

    def launchplane_is_killed_once(
        attempt: int, stands: Stands, answer: dict[str, Any] | None, error: str | None, *, decided: Verdict | None = None
    ) -> NoReturn:
        record.end = end
        raise LaunchplaneKilled(attempt, stands, answer, error, decided)

    record.end = launchplane_is_killed_once
    worker = Worker(record, lanes, NO_SECRETS)
    with pytest.raises(LaunchplaneKilled):
        worker.work_next()
    proxy.host.actions.clear()
    assert Worker(record, lanes, NO_SECRETS).work_next() is True
    kept = kept_of(record, request.id)
    assert kept.error is not None and "exited with code 137" in kept.error
    assert (kept.stands, [each.ended for each in kept.attempts], proxy.host.actions) == (
        Stands.FAILED,
        ["interrupted", "failed"],
        [],
    )


def test_no_cancel_removes_the_instance_the_proxy_sends_visitors_to_though_its_route_names_no_request(record: Record) -> None:
    worker, _, proxy = serving_a(record)
    request = taken_up_and_left(record, MOVE_B)
    proxy.host.place(TO_BUILD_B, "switched-to", generation=kept_of(record, request).generation)
    proxy.route(WEB_ADDRESS, "switched-to")
    record.cancel(request, BY_HAND)
    assert worker.work_next() is True
    kept = kept_of(record, request)
    assert kept.error is not None and f"the proxy of {WEB_ADDRESS} names its instance switched-to" in kept.error
    assert (kept.stands, "switched-to" in proxy.host.instances, proxy.host.actions) == (Stands.WORKING, True, [])


def test_what_was_put_back_is_told_from_every_attempt_when_the_record_was_lost_after_the_first_removed_it() -> None:
    record = PretendRecord()
    worker, _, proxy = serving_a(record)
    request = record.write(MOVE_B, CALLER, None)
    proxy.while_checking = cancelling(record, request.id)
    end = record.end

    def lose_the_record_once(
        attempt: int, stands: Stands, answer: dict[str, Any] | None, error: str | None, *, decided: Verdict | None = None
    ) -> NoReturn:
        record.end = end
        raise RecordError(
            "Launchplane's record could not be reached while writing how a request ended: "
            f"{attempt} {stands} {answer} {error} {decided}"
        )

    record.end = lose_the_record_once
    with pytest.raises(RecordError):
        worker.work_next()
    assert worker.work_next() is True
    kept = kept_of(record, request.id)
    assert (kept.stands, [each.ended for each in kept.attempts], put_back_by(kept)) == (
        Stands.CANCELLED,
        ["interrupted", "cancelled"],
        [f"retire {NEW}"],
    )


def test_ending_maintenance_cancelled_over_a_database_that_had_to_be_killed_does_not_end_as_cancelled_and_says_why(
    record: Record,
) -> None:
    proxy = lane_in_maintenance()
    proxy.host.databases_that_must_be_killed.add("database-data-1")
    request = record.write(END, CALLER, None)
    proxy.while_checking = cancelling(record, request.id)
    assert Worker(record, lanes_on(proxy), NO_SECRETS).work_next() is True
    kept = kept_of(record, request.id)
    assert kept.error is not None and "left a part not as it must be" in kept.error and "exited with code 137" in kept.error
    assert (kept.stands, proxy.written) == (Stands.FAILED, {WEB_ADDRESS: BACK_SOON})


def test_a_retire_cancelled_before_it_deleted_the_named_data_set_keeps_it_and_finishes_the_rest_as_asked(record: Record) -> None:
    proxy = lane_brought_up_with_a()
    proxy.host.actions.clear()
    request = record.write(Asked(product="site", lane="testing", work=Work.RETIRE, delete_data_set="data-1"), CALLER, None)
    proxy.while_awaiting = cancelling(record, request.id)
    assert Worker(record, lanes_on(proxy), NO_SECRETS).work_next() is True
    kept = kept_of(record, request.id)
    assert kept.answer is not None and kept.cancel_refused is not None
    assert "The data set data-1, named for deleting, is kept" in kept.cancel_refused
    assert (kept.stands, kept.answer["outcome"], kept.answer["kept_data_sets"], kept.answer["deleted_data_set"]) == (
        Stands.DONE,
        "retired",
        ["data-1"],
        None,
    )
    assert (list(proxy.host.data_sets), proxy.host.instances) == (["data-1"], {})


def test_asking_with_a_key_for_exactly_what_an_unfinished_request_asks_cancels_nothing_and_writes_a_request_of_its_own(
    record: Record,
) -> None:
    waiting, other = record.write(MOVE_B, CALLER, None), record.write(RETIRE, CALLER, None)
    with_a_key = record.write(RETIRE, CALLER, "one-release")
    assert (with_a_key.id != other.id, kept_of(record, other.id).stands, kept_of(record, other.id).cancel_asked_by) == (
        True,
        Stands.WAITING,
        None,
    )
    assert kept_of(record, waiting.id).cancel_asked_by == f"request {other.id}"


def test_a_cancel_that_waits_is_answered_at_once_for_a_request_that_needs_a_person(record: Record) -> None:
    bed = bed_on(record)
    stuck = bed.client.post(MOVE, json=TO_B).json()["id"]
    taken_up_until_it_needs_a_person(record, stuck)
    began = time.monotonic()
    answer = bed.client.post(f"/requests/{stuck}/cancel?wait=5")
    assert (answer.status_code, time.monotonic() - began < 1) == (409, True)


def test_a_cancel_that_carries_a_secret_a_container_holds_is_refused_and_marks_nothing(
    record: Record, secrets: GivenSecrets
) -> None:
    bed = bed_on(record)
    asked = bed.client.post(MOVE, json=TO_B).json()["id"]
    assert bed.record.take_up(asked) is not None
    secrets.seen = frozenset({"testclient"})
    refused = bed.client.post(f"/requests/{asked}/cancel")
    assert (refused.status_code, kept_of(record, asked).cancel_asked_by) == (422, None)


def row_of(bed: Bed, request: int) -> tuple[list[str], str]:
    [card] = bed.client.get("/running").json()["cards"]
    [row] = [row for row in card["rows"] if row["name"] == f"Request {request}"]
    return [pill["text"] for pill in row["pills"]], row["note"]


CANCEL_ON_RECORD = {
    "not yet carried to the gate": (None, "the gate has not been asked yet, and its worker carries the cancel there."),
    "with a gate that gives no answer": (
        "The routes folder could not be read",
        "the gate could not be asked whether that came in time.",
    ),
}


@pytest.mark.parametrize(("unreadable", "said"), CANCEL_ON_RECORD.values(), ids=CANCEL_ON_RECORD.keys())
def test_a_working_move_with_a_cancel_on_record_shows_what_the_gate_answers_for_it(
    record: Record, unreadable: str | None, said: str
) -> None:
    bed = bed_on(record)
    asked = bed.client.post(MOVE, json=TO_B).json()["id"]
    notes: list[str] = []

    def cancel_on_record_alone(_container: str) -> None:
        bed.record.cancel(asked, BY_HAND)
        bed.proxy.gate.unreadable = unreadable
        notes.append(row_of(bed, asked)[1])
        bed.proxy.gate.unreadable = None

    bed.proxy.while_checking = cancel_on_record_alone
    assert bed.worker.worked_one() is True
    assert [note.endswith(f"To be cancelled by {BY_HAND}: {said}") for note in notes] == [True]


def test_a_cancelled_request_shows_on_its_lanes_card_and_among_the_ended_with_what_was_put_back_and_by_whom(
    record: Record,
) -> None:
    bed = bed_on(record)
    asked = bed.client.post(MOVE, json=TO_B).json()["id"]
    while_working: list[tuple[int, list[str], str]] = []

    def cancel_by_the_call(_container: str) -> None:
        answer = bed.client.post(f"/requests/{asked}/cancel")
        while_working.append((answer.status_code, *row_of(bed, asked)))

    bed.proxy.while_checking = cancel_by_the_call
    assert bed.worker.worked_one() is True
    by = "a call from the address testclient"
    [(marked, pills_meanwhile, note_meanwhile)] = while_working
    [ended] = bed.client.get("/requests").json()["ended"]
    assert (
        marked,
        pills_meanwhile,
        note_meanwhile.endswith(f"To be cancelled by {by}: the cancel won at the gate, and its worker puts the lane back."),
    ) == (
        202,
        ["working"],
        True,
    )
    assert row_of(bed, asked) == (
        ["cancelled"],
        f"The move of testing of site to site:b was cancelled by {by}. Done to put the lane back: retire {NEW}.",
    )
    assert (ended["stands"], ended["answer"]["put_back"], ended["cancel_asked_by"]) == ("cancelled", [f"retire {NEW}"], by)
    assert bed.client.get("/running").json()["cancels"][0]["id"] == asked


def held_check(bed: Bed) -> tuple[threading.Event, threading.Event]:
    checking, go_on = threading.Event(), threading.Event()

    def hold_the_check(_container: str) -> None:
        checking.set()
        assert go_on.wait(timeout=5)

    bed.proxy.while_checking = hold_the_check
    return checking, go_on


def test_the_cancel_call_answers_how_the_request_stands_at_once_and_how_it_ended_when_it_waits(record: Record) -> None:
    bed = bed_on(record)
    checking, go_on = held_check(bed)
    threads = bed.worker.start()
    asked = bed.client.post(MOVE, json=TO_B).json()["id"]
    assert checking.wait(timeout=5)
    at_once = bed.client.post(f"/requests/{asked}/cancel")
    go_on.set()
    with_a_wait = bed.client.post(f"/requests/{asked}/cancel?wait=5")
    again = bed.client.post(f"/requests/{asked}/cancel")
    stopped(bed.worker, threads)
    assert [(each.status_code, each.json()["stands"]) for each in (at_once, with_a_wait, again)] == [
        (202, "working"),
        (200, "cancelled"),
        (200, "cancelled"),
    ]
    assert bed.client.post("/requests/999/cancel").status_code == 404


def test_a_call_that_waits_for_a_request_that_is_then_cancelled_answers_conflict_and_says_by_whom(record: Record) -> None:
    bed = bed_on(record)
    the_first_request = 1
    bed.proxy.while_checking = cancelling(record, the_first_request)
    threads = bed.worker.start()
    waited = bed.client.post(f"{MOVE}?wait=5", json=TO_B)
    stopped(bed.worker, threads)
    assert (waited.status_code, waited.headers["location"]) == (409, f"/requests/{the_first_request}")
    assert f"was cancelled by {BY_HAND}. Done to put the lane back: retire {NEW}." in waited.json()["error"]


def test_the_cancel_call_says_so_when_it_is_too_late_or_the_request_had_ended_or_needs_a_person(record: Record) -> None:
    bed = bed_on(record)
    switched, go_on = threading.Event(), threading.Event()

    def hold_once_switched() -> None:
        switched.set()
        assert go_on.wait(timeout=5)

    bed.proxy.while_awaiting = hold_once_switched
    threads = bed.worker.start()
    asked = bed.client.post(MOVE, json=TO_B).json()["id"]
    assert switched.wait(timeout=5)
    too_late: list[tuple[int, dict[str, Any]]] = []

    def cancel_and_wait() -> None:
        answer = bed.client.post(f"/requests/{asked}/cancel?wait=5")
        too_late.append((answer.status_code, answer.json()))

    caller = threading.Thread(target=cancel_and_wait)
    caller.start()
    wait_until(lambda: kept_of(record, asked).cancel_asked_by is not None)
    go_on.set()
    caller.join(timeout=5)
    wait_until(lambda: kept_of(record, asked).stands is Stands.DONE)
    stopped(bed.worker, threads)
    ended = bed.client.post(f"/requests/{asked}/cancel")
    stuck = bed.client.post(MOVE, json={"build": "site:c"}).json()["id"]
    taken_up_until_it_needs_a_person(record, stuck)
    needs_a_person = bed.client.post(f"/requests/{stuck}/cancel")
    assert [too_late[0][0], ended.status_code, needs_a_person.status_code] == [409, 409, 409]
    assert "already renamed for this release" in too_late[0][1]["error"]
    assert f"Request {asked} ended as done" in ended.json()["error"]
    assert f"POST /requests/{stuck}/give-up" in needs_a_person.json()["error"]


def test_a_cancel_recorded_while_the_host_is_read_for_the_switch_is_seen_before_the_switch_is_decided(record: Record) -> None:
    worker, _, proxy = serving_a(record)
    request = record.write(MOVE_B, CALLER, None)
    read, cancelled = proxy.host.read, list[bool]()

    def read_while_a_cancel_is_recorded(lane: tuple[str, str] | None = None) -> OnHost:
        on_host = read(lane)
        if proxy.host.actions[-1:] == [f"check {NEW}/"] and not cancelled:
            cancelled.append(True)
            record.cancel(request.id, BY_HAND)
        return on_host

    proxy.host.read = read_while_a_cancel_is_recorded
    assert worker.work_next() is True
    kept = kept_of(record, request.id)
    assert (kept.stands, proxy.written, list(proxy.host.instances)) == (Stands.CANCELLED, {WEB_ADDRESS: "a"}, ["a"])


def test_a_proxy_that_does_not_load_a_renamed_route_leaves_the_release_working_however_long_and_it_is_finished_once_it_loads(
    record: Record,
) -> None:
    worker, _, proxy = serving_a(record)
    request = record.write(MOVE_B, CALLER, None)
    proxy.holds_delivery = True
    assert [worker.work_next() for _ in range(2 * MOST_ATTEMPTS)] == [True] * 2 * MOST_ATTEMPTS
    held = kept_of(record, request.id)
    assert held.error is not None and "did not load the routes file" in held.error
    assert (
        held.stands,
        {each.ended for each in held.attempts},
        proxy.loaded[WEB_ADDRESS].container,
        sorted(proxy.host.instances),
    ) == (
        Stands.WORKING,
        {"unfinished"},
        "a",
        ["a", NEW],
    )
    proxy.holds_delivery = False
    assert (worker.work_next(), worker.work_next()) == (True, False)
    kept = kept_of(record, request.id)
    assert kept.answer is not None
    assert (kept.stands, kept.answer["outcome"], list(proxy.host.instances)) == (Stands.DONE, "switched", [NEW])


class GateThatCannotBeReached(GateNeverAsked):
    def abandon(self, product: str, lane: str, generation: str, /) -> NoReturn:
        raise GateError(f"The gate gave no verdict for the release {generation} of {lane} of {product}: connection refused")


def test_a_cancel_that_cannot_reach_the_gate_says_so_and_the_worker_carries_it_there_and_puts_the_lane_back(
    record: Record,
) -> None:
    worker, lanes, proxy = serving_a(record)
    client = TestClient(create_app(lanes.read_status, record, worker.wake, NO_SECRETS, GateThatCannotBeReached()))
    asked = client.post(MOVE, json=TO_B).json()["id"]
    answers: list[tuple[int, str]] = []

    def cancel_while_checking(_container: str) -> None:
        answer = client.post(f"/requests/{asked}/cancel")
        answers.append((answer.status_code, answer.json()["error"]))

    proxy.while_checking = cancel_while_checking
    assert worker.work_next() is True
    [(status, error)] = answers
    assert (status, "whether that came in time is not known yet" in error, "Its worker carries the cancel on" in error) == (
        503,
        True,
        True,
    )
    assert (
        kept_of(record, asked).stands,
        proxy.gate.verdict(WEB_ADDRESS, kept_of(record, asked).generation),
        list(proxy.host.instances),
    ) == (
        Stands.CANCELLED,
        GateAnswer(Verdict.ABANDONED),
        ["a"],
    )


def test_a_newer_request_whose_cancel_of_the_older_cannot_reach_the_gate_is_kept_says_so_and_the_worker_carries_the_cancel(
    record: Record,
) -> None:
    worker, lanes, proxy = serving_a(record)
    client = TestClient(create_app(lanes.read_status, record, worker.wake, NO_SECRETS, GateThatCannotBeReached()))
    older = client.post(MOVE, json=TO_B).json()["id"]
    newer: list[tuple[int, str, str]] = []

    def ask_for_c_while_b_is_checked(_container: str) -> None:
        if not newer:
            answer = client.post(MOVE, json={"build": "site:c"})
            newer.append((answer.status_code, answer.headers["location"], answer.json()["error"]))

    proxy.while_checking = ask_for_c_while_b_is_checked
    while worker.work_next():
        pass
    [(status, location, error)] = newer
    kept = kept_of(record, int(location.rsplit("/", 1)[1]))
    assert (status, "is kept and will be worked" in error, "could not be carried to the gate" in error) == (503, True, True)
    assert (kept_of(record, older).stands, kept.stands) == (Stands.CANCELLED, Stands.DONE)
    assert [each.build for each in proxy.host.instances.values()] == ["site:c"]


def test_ending_maintenance_on_a_lane_already_out_ends_as_it_would_have_over_a_cancel_recorded_just_before(
    record: Record,
) -> None:
    worker, _, proxy = serving_a(record)
    request = record.write(END, CALLER, None)
    end = record.end

    def end_just_after_a_cancel(
        attempt: int, stands: Stands, answer: dict[str, Any] | None, error: str | None, *, decided: Verdict | None = None
    ) -> bool:
        record.cancel(request.id, BY_HAND)
        return end(attempt, stands, answer, error, decided=decided)

    record.end = end_just_after_a_cancel
    assert worker.work_next() is True
    kept = kept_of(record, request.id)
    assert (kept.stands, kept.cancel_asked_by, kept.cancel_refused, proxy.host.actions) == (Stands.DONE, BY_HAND, None, [])


def test_a_left_over_candidate_whose_rename_lands_while_recovery_is_cleaning_it_up_is_kept_and_its_release_finished(
    record: Record,
) -> None:
    worker, lanes, proxy = serving_a(record)
    request = killed_while_checking(record, lanes, proxy)
    discard = proxy.gate.discard

    def the_rename_sent_before_the_kill_lands_first(web_address: str, generation: str, attempt: int, candidate: str) -> GateAnswer:
        if proxy.written[web_address] == "a":
            proxy.gate.switch(web_address, generation, 0, NEW)
            proxy.loaded[web_address] = LoadedRoute(NEW, RouteStatus.ENABLED)
        return discard(web_address, generation, attempt, candidate)

    proxy.gate.discard = the_rename_sent_before_the_kill_lands_first
    assert worker.work_next() is True
    kept = kept_of(record, request)
    assert kept.answer is not None
    assert (kept.stands, kept.answer["outcome"], proxy.written, list(proxy.host.instances)) == (
        Stands.DONE,
        "switched",
        {WEB_ADDRESS: NEW},
        [NEW],
    )


def test_a_switch_whose_reply_is_lost_is_asked_of_the_gate_and_the_release_is_finished_as_switched_with_one_rename(
    record: Record,
) -> None:
    worker, _, proxy = serving_a(record)
    request = record.write(MOVE_B, CALLER, None)
    switch, lost = proxy.switch, list[bool]()

    def switch_and_lose_the_reply(web_address: str, generation: str, attempt: int, candidate: str) -> GateAnswer:
        answer = switch(web_address, generation, attempt, candidate)
        if not lost:
            lost.append(True)
            raise ContainerHostError("Running flock in launchplane-proxy on test failed: the connection was lost")
        return answer

    proxy.switch = switch_and_lose_the_reply
    while worker.work_next():
        pass
    kept = kept_of(record, request.id)
    assert kept.answer is not None
    assert (kept.stands, kept.answer["outcome"], [each.ended for each in kept.attempts], list(proxy.host.instances)) == (
        Stands.DONE,
        "switched",
        ["unfinished", "done"],
        [NEW],
    )
    assert [action for action in proxy.host.actions if action.startswith("route")] == [f"route {NEW}"]


def test_a_cancel_on_record_with_a_host_that_cannot_be_read_wins_at_the_gate_and_nothing_is_ended_until_the_host_reads(
    record: Record,
) -> None:
    worker, lanes, proxy = serving_a(record)
    request = killed_while_checking(record, lanes, proxy)
    read, readings = proxy.host.read, list[int]()

    def unreadable(lane: tuple[str, str] | None = None) -> OnHost:
        readings.append(1)
        raise ContainerHostError(f"Listing the containers of {lane} on test failed: connection refused")

    proxy.host.read = unreadable
    record.cancel(request, BY_HAND)
    assert [worker.work_next() for _ in range(2 * MOST_ATTEMPTS)] == [True] * 2 * MOST_ATTEMPTS
    during = kept_of(record, request)
    assert during.error is not None and "connection refused" in during.error
    assert (during.stands, len(readings) <= 4 * MOST_ATTEMPTS, sorted(proxy.host.instances)) == (Stands.WORKING, True, ["a", NEW])
    proxy.host.read = read
    assert (worker.work_next(), worker.work_next()) == (True, False)
    assert (kept_of(record, request).stands, list(proxy.host.instances)) == (Stands.CANCELLED, ["a"])


def test_a_cancel_that_won_and_whose_candidate_docker_refuses_to_remove_ends_as_failed_saying_it_won_and_what_is_left(
    record: Record,
) -> None:
    worker, lanes, proxy = serving_a(record)
    client = TestClient(create_app(lanes.read_status, record, worker.wake, NO_SECRETS, lanes))
    asked = client.post(MOVE, json=TO_B).json()["id"]

    def refuse(container: str) -> NoReturn:
        raise ContainerHostRefused(f"Stopping and removing {container} on test was refused: 409 Conflict")

    def cancel_while_checking(_container: str) -> None:
        proxy.host.retire_instance = refuse
        client.post(f"/requests/{asked}/cancel")

    proxy.while_checking = cancel_while_checking
    assert (worker.work_next(), worker.work_next()) == (True, False)
    kept, again = kept_of(record, asked), client.post(f"/requests/{asked}/cancel")
    assert kept.error is not None and kept.error.startswith(f"The cancel won, and {NEW} could not be removed")
    assert (kept.stands, kept.cancel_refused, again.status_code, kept.error in again.json()["error"]) == (
        Stands.FAILED,
        None,
        409,
        True,
    )
    assert (proxy.written, sorted(proxy.host.instances)) == ({WEB_ADDRESS: "a"}, ["a", NEW])
    assert request_note(kept, {}) == kept.error


def test_giving_up_a_request_that_needs_a_person_and_whose_release_is_committed_is_refused_and_it_is_worked_again(
    record: Record,
) -> None:
    worker, lanes, proxy = serving_a(record)
    client = TestClient(create_app(lanes.read_status, record, worker.wake, NO_SECRETS, lanes))
    asked = client.post(MOVE, json=TO_B).json()["id"]
    taken_up_until_it_needs_a_person(record, asked)
    proxy.host.place(TO_BUILD_B, NEW, generation=kept_of(record, asked).generation)
    proxy.gate.switch(WEB_ADDRESS, kept_of(record, asked).generation, 0, NEW)
    refused = [client.post(f"/requests/{asked}/give-up"), client.post(f"/requests/{asked}/retry")]
    assert ([each.status_code for each in refused], kept_of(record, asked).stands) == ([409, 409], Stands.WORKING)
    while worker.work_next():
        pass
    kept = kept_of(record, asked)
    assert kept.answer is not None
    assert (kept.stands, kept.answer["outcome"], list(proxy.host.instances)) == (Stands.DONE, "switched", [NEW])


def test_giving_up_a_request_that_needs_a_person_and_whose_cancel_won_is_refused_and_its_lane_is_put_back(record: Record) -> None:
    worker, lanes, proxy = serving_a(record)
    client = TestClient(create_app(lanes.read_status, record, worker.wake, NO_SECRETS, lanes))
    asked = client.post(MOVE, json=TO_B).json()["id"]
    taken_up_until_it_needs_a_person(record, asked)
    proxy.host.place(TO_BUILD_B, NEW, generation=kept_of(record, asked).generation)
    proxy.gate.abandon(WEB_ADDRESS, kept_of(record, asked).generation)
    refused = [client.post(f"/requests/{asked}/give-up"), client.post(f"/requests/{asked}/retry")]
    assert ([each.status_code for each in refused], kept_of(record, asked).stands) == ([409, 409], Stands.WORKING)
    while worker.work_next():
        pass
    assert (kept_of(record, asked).stands, list(proxy.host.instances)) == (Stands.CANCELLED, ["a"])


def interrupted_as_often_as_a_request_may_be(record: Record) -> Request:
    request = record.write(MOVE_B, CALLER, None)
    for _ in range(MOST_ATTEMPTS):
        assert record.take_up(request.id) is not None
    return request


def test_a_move_at_the_limit_needs_a_person_only_once_its_last_attempt_is_fenced_so_its_switch_can_no_longer_rename(
    record: Record,
) -> None:
    worker, _, proxy = serving_a(record)
    request = interrupted_as_often_as_a_request_may_be(record)
    proxy.host.place(TO_BUILD_B, NEW, generation=request.generation)
    assert worker.work_next() is True
    late = proxy.gate.switch(WEB_ADDRESS, request.generation, MOST_ATTEMPTS, NEW)
    assert (kept_of(record, request.id).stands, late, proxy.written) == (Stands.NEEDS_A_PERSON, NO_VERDICT, {WEB_ADDRESS: "a"})


AT_THE_LIMIT: dict[str, tuple[Callable[[PretendGate, str], object], Stands, list[str]]] = {
    "committed is finished": (lambda gate, generation: gate.switch(WEB_ADDRESS, generation, 1, NEW), Stands.DONE, [NEW]),
    "a cancel that won is put back": (lambda gate, generation: gate.abandon(WEB_ADDRESS, generation), Stands.CANCELLED, ["a"]),
}


@pytest.mark.parametrize(("decide", "stands", "instances"), AT_THE_LIMIT.values(), ids=AT_THE_LIMIT.keys())
def test_a_move_at_the_limit_whose_release_the_gate_has_decided_is_taken_up_however_often_it_was_interrupted(
    record: Record, decide: Callable[[PretendGate, str], object], stands: Stands, instances: list[str]
) -> None:
    worker, _, proxy = serving_a(record)
    request = interrupted_as_often_as_a_request_may_be(record)
    proxy.host.place(TO_BUILD_B, NEW, generation=request.generation)
    decide(proxy.gate, request.generation)
    while worker.work_next():
        pass
    assert (kept_of(record, request.id).stands, list(proxy.host.instances)) == (stands, instances)


def test_a_move_at_the_limit_with_a_cancel_on_record_is_not_left_to_a_person_and_the_cancel_is_carried_out(record: Record) -> None:
    worker, _, proxy = serving_a(record)
    request = interrupted_as_often_as_a_request_may_be(record)
    record.cancel(request.id, BY_HAND)
    while worker.work_next():
        pass
    assert (kept_of(record, request.id).stands, list(proxy.host.instances)) == (Stands.CANCELLED, ["a"])


def test_a_move_at_the_limit_while_the_gate_cannot_be_asked_is_not_left_to_a_person(record: Record) -> None:
    worker, _, proxy = serving_a(record)
    request = interrupted_as_often_as_a_request_may_be(record)
    proxy.gate.unreadable = "The routes folder could not be read"
    assert [worker.work_next() for _ in range(3)] == [True] * 3
    during = kept_of(record, request.id)
    assert (during.stands, [each.ended for each in during.attempts][MOST_ATTEMPTS:]) == (Stands.WORKING, ["unfinished"] * 3)
    proxy.gate.unreadable = None
    while worker.work_next():
        pass
    assert kept_of(record, request.id).stands is Stands.NEEDS_A_PERSON


def test_a_call_to_the_host_that_got_no_answer_before_the_switch_leaves_the_move_working_and_it_is_finished_when_it_does(
    record: Record,
) -> None:
    worker, _, proxy = serving_a(record)
    switch, lost = proxy.switch, [True]

    def switch_whose_reply_is_lost_once(web_address: str, generation: str, attempt: int, candidate: str) -> GateAnswer:
        if lost.pop() if lost else False:
            raise ContainerHostError("Running flock in the proxy on test failed: timed out")
        return switch(web_address, generation, attempt, candidate)

    proxy.switch = switch_whose_reply_is_lost_once
    request = record.write(MOVE_B, CALLER, None)
    assert worker.work_next() is True
    during = kept_of(record, request.id)
    assert (during.stands, [each.ended for each in during.attempts]) == (Stands.WORKING, ["unfinished"])
    while worker.work_next():
        pass
    kept = kept_of(record, request.id)
    assert (kept.stands, [each.ended for each in kept.attempts], len(proxy.host.instances)) == (
        Stands.DONE,
        ["unfinished", "done"],
        1,
    )


def refusing(what: str) -> Callable[..., NoReturn]:
    def refuse(*_given: object) -> NoReturn:
        raise ContainerHostRefused(f"{what} on test was refused: 409 Conflict")

    return refuse


def test_a_move_the_host_refuses_before_the_switch_ends_failed_only_once_its_candidate_is_removed(record: Record) -> None:
    worker, _, proxy = serving_a(record)
    proxy.switch = refusing("The switch")
    request = record.write(MOVE_B, CALLER, None)
    assert (worker.work_next(), worker.work_next()) == (True, False)
    kept = kept_of(record, request.id)
    assert (kept.stands, list(proxy.host.instances), proxy.gate.of(request.generation).fenced) == (Stands.FAILED, ["a"], 1)


def test_a_move_that_failed_before_the_switch_and_whose_candidate_cannot_be_removed_stays_working(record: Record) -> None:
    worker, _, proxy = serving_a(record)
    proxy.switch = refusing("The switch")
    proxy.host.retire_instance = refusing("Stopping and removing the candidate")
    request = record.write(MOVE_B, CALLER, None)
    assert worker.work_next() is True
    kept = kept_of(record, request.id)
    assert kept.error is not None and "could not be removed" in kept.error
    assert (kept.stands, sorted(proxy.host.instances)) == (Stands.WORKING, ["a", NEW])


def test_a_move_that_failed_with_a_cancel_on_record_the_gate_had_not_decided_ends_cancelled_and_never_failed(
    record: Record,
) -> None:
    worker, _, proxy = serving_a(record)
    request = record.write(MOVE_B, CALLER, None)
    abandon, refused = proxy.gate.abandon, [True]

    def abandon_refused_once(web_address: str, generation: str) -> GateAnswer:
        if refused.pop() if refused else False:
            raise ContainerHostRefused("Running flock in the proxy on test was refused: 409 Conflict")
        return abandon(web_address, generation)

    proxy.gate.abandon = abandon_refused_once

    def cancel_while_checking(_container: str) -> None:
        record.cancel(request.id, BY_HAND)

    proxy.while_checking = cancel_while_checking
    while worker.work_next():
        pass
    kept = kept_of(record, request.id)
    assert (kept.stands, kept.cancel_refused, list(proxy.host.instances)) == (Stands.CANCELLED, None, ["a"])
