import threading
from collections.abc import Callable
from typing import Any

import pytest

from launchplane.act import Lanes
from launchplane.instance import BACK_SOON, InstanceState, LaneBuild
from launchplane.judge import Work
from launchplane.record import Asked, Record, Refused, Stands
from launchplane.telling import Witness
from launchplane.worker import Worker
from tests.given_secrets import NO_SECRETS, GivenSecrets
from tests.pretend_host import WEB_ADDRESS, WITH_DATA, LaunchplaneKilled, PretendHost, PretendProxy, numbered_names
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


# What a test gives the pretend proxy to call at the moment the cancel is to arrive.
def cancelling(record: Record, request: int) -> Callable[..., None]:
    def cancel(*_at: object) -> None:
        record.cancel(request, BY_HAND)

    return cancel


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
    assert (cancelled.stands, cancelled.answer, cancelled.cancel_asked_by) == (Stands.CANCELLED, {"put_back": []}, BY_HAND)
    assert (worker.work_next(), proxy.host.actions, kept_of(record, request.id).attempts) == (False, [], [])


def test_a_move_cancelled_before_the_switch_removes_the_instance_it_started_and_visitors_stay_where_they_were(
    record: Record,
) -> None:
    worker, _, proxy = serving_a(record)
    request = record.write(MOVE_B, CALLER, None)
    proxy.while_checking = cancelling(record, request.id)
    assert worker.work_next() is True
    kept = kept_of(record, request.id)
    assert (kept.stands, kept.answer, kept.cancel_asked_by, kept.error) == (
        Stands.CANCELLED,
        {"put_back": [f"removed {NEW}"]},
        BY_HAND,
        None,
    )
    assert (proxy.written, list(proxy.host.instances)) == ({WEB_ADDRESS: "a"}, ["a"])
    assert ([each.ended for each in kept.attempts], kept.events[-1].did) == (["cancelled"], f"remove {NEW}")


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
    assert f"already sends its visitors to {NEW}" in kept.cancel_refused and "finished as asked" in kept.cancel_refused


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
    assert (proxy.host.actions, kept.answer, proxy.written) == (
        [f"remove {NEW}"],
        {"put_back": [f"removed {NEW}"]},
        {WEB_ADDRESS: "a"},
    )


def test_a_move_killed_just_after_the_switch_is_too_late_to_cancel_though_no_event_says_it_switched(record: Record) -> None:
    worker, lanes, proxy = serving_a(record)
    request = taken_up_and_left(record, MOVE_B)
    proxy.host.killed_after = len(["start", "check", "route"])
    with pytest.raises(LaunchplaneKilled):
        lanes.move(TO_BUILD_B, Witness())
    proxy.host.killed_after = None
    record.cancel(request, BY_HAND)
    assert worker.work_next() is True
    kept = kept_of(record, request)
    assert kept.cancel_refused is not None and "already sends its visitors" in kept.cancel_refused
    assert (kept.stands, list(proxy.host.instances)) == (Stands.DONE, [NEW])


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
    assert (kept.stands, kept.answer, proxy.host.actions, proxy.written) == (
        Stands.CANCELLED,
        {"put_back": []},
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
    assert (kept.stands, kept.answer, proxy.host.actions) == (
        Stands.CANCELLED,
        {"put_back": ["stopped a"]},
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


def test_putting_a_lane_back_leaves_the_data_set_the_cancelled_move_made(record: Record) -> None:
    proxy = PretendProxy(PretendHost())
    lanes = Lanes(proxy.host, proxy, read_product=lambda _product: WITH_DATA, name_fresh=numbered_names())
    request = record.write(MOVE_B, CALLER, None)
    proxy.while_checking = cancelling(record, request.id)
    assert Worker(record, lanes, NO_SECRETS).work_next() is True
    deleted = [action for action in proxy.host.actions if action.startswith("delete")]
    assert (kept_of(record, request.id).stands, proxy.host.instances, deleted) == (Stands.CANCELLED, {}, [])
    assert len(proxy.host.data_sets) == 1


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


def test_a_cancelled_request_shows_on_its_lanes_card_and_among_the_ended_with_what_was_put_back_and_by_whom(
    record: Record,
) -> None:
    bed = bed_on(record)
    asked = bed.client.post(MOVE, json=TO_B).json()["id"]
    while_working: list[tuple[int, list[str], str]] = []

    def cancel_by_the_call(_container: str) -> None:
        marked = bed.client.post(f"/requests/{asked}/cancel")
        while_working.append((marked.status_code, *row_of(bed, asked)))

    bed.proxy.while_checking = cancel_by_the_call
    assert bed.worker.worked_one() is True
    by = "a call from the address testclient"
    [(marked, pills_meanwhile, note_meanwhile)] = while_working
    [ended] = bed.client.get("/requests").json()["ended"]
    assert (
        marked,
        pills_meanwhile,
        note_meanwhile.endswith(f"To be cancelled by {by}: its worker reads that before its next action."),
    ) == (
        202,
        ["working"],
        True,
    )
    assert row_of(bed, asked) == (
        ["cancelled"],
        f"The move of testing of site to site:b was cancelled by {by}. To put the lane back, Launchplane removed {NEW}.",
    )
    assert (ended["stands"], ended["answer"], ended["cancel_asked_by"]) == ("cancelled", {"put_back": [f"removed {NEW}"]}, by)
    assert bed.client.get("/running").json()["cancelled"][0]["id"] == asked


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
    assert f"was cancelled by {BY_HAND}. To put the lane back, Launchplane removed {NEW}." in waited.json()["error"]


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
    assert "already sends its visitors" in too_late[0][1]["error"]
    assert f"Request {asked} ended as done" in ended.json()["error"]
    assert f"POST /requests/{stuck}/give-up" in needs_a_person.json()["error"]
