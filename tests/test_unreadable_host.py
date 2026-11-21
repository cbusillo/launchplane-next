from collections.abc import Callable
from typing import NoReturn

import pytest
from fastapi.testclient import TestClient

from launchplane.act import Lanes
from launchplane.api import create_app
from launchplane.docker_answers import ContainerHostError, ContainerHostRefused
from launchplane.gate import LOST
from launchplane.instance import BACK_SOON, InstanceState, OnHost
from launchplane.record import Asked, Kept, Record, Request, Stands
from launchplane.steps import NOT_KNOWN
from launchplane.telling import NOBODY
from launchplane.worker import Worker, carried_out
from tests.given_secrets import NO_SECRETS
from tests.pretend_host import WEB_ADDRESS, PretendHost, PretendProxy, numbered_names
from tests.test_act import lane_brought_up_with_a, lane_in_maintenance, lanes_on
from tests.test_host_answers import (
    BEGIN,
    DATABASE,
    END,
    MOVE_A,
    NOT_TOLD,
    RETIRE,
    lane_whose_background_service_is_gone,
    worker_on,
)
from tests.test_worker import CALLER, MOVE_B, kept_of, serving_a


def lane_retired() -> PretendProxy:
    proxy = lane_brought_up_with_a()
    lanes_on(proxy).retire("site", "testing")
    proxy.host.actions.clear()
    return proxy


def parts_up(proxy: PretendProxy) -> list[str]:
    parts = (*proxy.host.instances.values(), *proxy.host.background_services.values())
    return sorted(part.container for part in parts if part.state != InstanceState.STOPPED)


def databases_up(proxy: PretendProxy) -> list[str]:
    databases = (database for found in proxy.host.data_sets.values() for database in found.databases)
    return sorted(database.container for database in databases if database.state != InstanceState.STOPPED)


def is_in_maintenance_with_everything_stopped(proxy: PretendProxy) -> bool:
    return (proxy.written.get(WEB_ADDRESS), parts_up(proxy), databases_up(proxy)) == (BACK_SOON, [], [])


def is_back_with_everything_up(proxy: PretendProxy) -> bool:
    serving = proxy.written.get(WEB_ADDRESS)
    return serving in proxy.host.instances and len(parts_up(proxy)) == 2 and len(databases_up(proxy)) == 1


def is_retired(proxy: PretendProxy) -> bool:
    return (WEB_ADDRESS in proxy.written, proxy.host.instances, proxy.host.background_services) == (False, {}, {})


@pytest.fixture(params=["a maintenance begin", "a maintenance end", "a retire"])
def kind(request: pytest.FixtureRequest) -> str:
    return request.param


# What is asked and the lane it is asked of. Then a change it sends that is no change of the route, and its first change.
# Then the lane on which nothing is left for it to do, what the host shows once it is done, and the outcome it answers.
type Lane = Callable[[], PretendProxy]
KINDS: dict[str, tuple[Asked, Lane, str, str, Lane, Callable[[PretendProxy], bool], str]] = {
    "a maintenance begin": (
        BEGIN,
        lane_brought_up_with_a,
        "stop_part",
        "edit",
        lane_in_maintenance,
        is_in_maintenance_with_everything_stopped,
        "in maintenance",
    ),
    "a maintenance end": (
        END,
        lane_in_maintenance,
        "start_part",
        "start_database",
        lane_brought_up_with_a,
        is_back_with_everything_up,
        "ended",
    ),
    "a retire": (RETIRE, lane_brought_up_with_a, "retire_instance", "edit", lane_retired, is_retired, "retired"),
}

UNREAD = "Reading what is on test failed: the connection was reset"


# The host answers every change and no reading, from the first change on, for as long as the test leaves it so.
def unread_after_the_first_change(proxy: PretendProxy, changes: int = 1) -> Callable[[], None]:
    read = proxy.host.read

    def not_read(lane: tuple[str, str] | None = None) -> OnHost:
        if len(proxy.host.actions) >= changes:
            raise ContainerHostError(UNREAD)
        return read(lane)

    def read_again() -> None:
        proxy.host.read = read

    proxy.host.read = not_read
    return read_again


# The change is made on the host, and its caller gets the error of a lost reply once.
def lose_the_first_reply_to(host: object, change: str) -> None:
    made = getattr(host, change)

    def made_and_not_answered(changed: object) -> None:
        setattr(host, change, made)
        made(changed)
        raise ContainerHostError(f"{change} on test failed: the connection was lost before the reply")

    setattr(host, change, made_and_not_answered)


def refuse(*_sent: object, **_named: object) -> NoReturn:
    raise ContainerHostRefused("The change on test was refused: pull access denied")


def left_working(record: Record, request: int) -> Kept:
    kept = kept_of(record, request)
    assert (kept.stands, {each.ended for each in kept.attempts}) == (Stands.WORKING, {"unfinished"}), kept
    return kept


def left_working_after_an_interruption(record: Record, request: int) -> str:
    kept = kept_of(record, request)
    assert (kept.stands, [each.ended for each in kept.attempts]) == (Stands.WORKING, ["interrupted", "unfinished"]), kept
    return (kept.error or "").rpartition(": ")[2]


def test_a_read_that_fails_after_the_first_change_leaves_the_request_working_with_its_lane_and_it_ends_done_once_the_host_is_read(
    record: Record, kind: str
) -> None:
    asked, lane, _, _, _, is_as_asked, outcome = KINDS[kind]
    proxy = lane()
    worker, read_again = worker_on(proxy, record), unread_after_the_first_change(proxy)
    request = record.write(asked, CALLER, None)
    assert worker.work_next() is True
    behind = record.write(MOVE_A, CALLER, None)
    for _ in range(5):
        assert worker.work_next() is True
    left = left_working(record, request.id)
    assert (left.error, len(left.attempts), kept_of(record, behind.id).stands) == (UNREAD, 6, Stands.WAITING)
    assert not is_as_asked(proxy)
    read_again()
    assert worker.work_next() is True
    ended = kept_of(record, request.id)
    assert (ended.stands, (ended.answer or {}).get("outcome"), is_as_asked(proxy)) == (Stands.DONE, outcome, True), ended


def test_a_change_that_is_made_and_whose_reply_is_lost_leaves_the_request_working_and_the_next_attempt_goes_on_from_the_host(
    record: Record, kind: str
) -> None:
    asked, lane, change, _, _, is_as_asked, outcome = KINDS[kind]
    proxy = lane()
    worker = worker_on(proxy, record)
    lose_the_first_reply_to(proxy.host, change)
    request = record.write(asked, CALLER, None)
    assert worker.work_next() is True
    left = left_working(record, request.id)
    assert "the connection was lost before the reply" in (left.error or "")
    made_once = list(proxy.host.actions)
    assert worker.work_next() is True
    ended = kept_of(record, request.id)
    assert (ended.stands, (ended.answer or {}).get("outcome"), is_as_asked(proxy)) == (Stands.DONE, outcome, True), ended
    assert [each for each in proxy.host.actions if each in made_once] == made_once


def test_a_first_change_the_host_refuses_ends_the_request_failed_with_the_hosts_error_and_its_lane_takes_the_next_request(
    record: Record, kind: str
) -> None:
    asked, lane, _, first, _, _, _ = KINDS[kind]
    proxy = lane()
    worker, changed = worker_on(proxy, record), proxy if first == "edit" else proxy.host
    answered = getattr(changed, first)
    setattr(changed, first, refuse)
    request = record.write(asked, CALLER, None)
    assert worker.work_next() is True
    setattr(changed, first, answered)
    again = record.write(asked, CALLER, None)
    assert worker.work_next() is True
    refused = kept_of(record, request.id)
    assert (refused.stands, [each.ended for each in refused.attempts], "pull access denied" in (refused.error or "")) == (
        Stands.FAILED,
        ["failed"],
        True,
    )
    assert (again.id != request.id, kept_of(record, again.id).stands) == (True, Stands.DONE)


def test_a_request_found_working_whose_fence_the_host_refuses_to_run_does_nothing_and_stays_working_until_the_cancels_decided(
    record: Record, kind: str
) -> None:
    asked, lane, _, _, _, is_as_asked, _ = KINDS[kind]
    proxy = lane()
    worker, fence = worker_on(proxy, record), proxy.gate.fence
    request = record.write(asked, CALLER, None)
    assert record.take_up(request.id) is not None
    proxy.gate.fence = refuse
    assert worker.work_next() is True
    assert (left_working_after_an_interruption(record, request.id), proxy.host.actions) == ("pull access denied", [])
    proxy.gate.fence = fence
    assert (worker.work_next(), kept_of(record, request.id).stands, is_as_asked(proxy)) == (True, Stands.DONE, True)


def test_a_request_whose_work_the_host_shows_done_ends_as_done_and_changes_nothing(record: Record, kind: str) -> None:
    asked, _, _, _, lane_as_asked, is_as_asked, _ = KINDS[kind]
    proxy = lane_as_asked()
    request = record.write(asked, CALLER, None)
    assert worker_on(proxy, record).work_next() is True
    assert (kept_of(record, request.id).stands, proxy.host.actions, is_as_asked(proxy)) == (Stands.DONE, [], True)


# The lane as this request leaves it when the host stops being read after so many changes: the request is at work, with
# no attempt of it open, and the host is read again.
def cut_short(record: Record, asked: Asked, lane: Lane, changes: int = 1) -> tuple[TestClient, PretendProxy, Worker, int]:
    proxy = lane()
    lanes = lanes_on(proxy)
    lanes.read_unfinished = record.unfinished
    worker, read_again = Worker(record, lanes, NO_SECRETS), unread_after_the_first_change(proxy, changes)
    request = record.write(asked, CALLER, None)
    assert worker.work_next() is True
    read_again()
    left_working(record, request.id)
    return TestClient(create_app(lanes.read_status, record, worker.wake, NO_SECRETS, lanes)), proxy, worker, request.id


def test_giving_up_a_request_whose_lane_cannot_be_read_answers_503_and_the_request_is_still_at_work(
    record: Record, kind: str
) -> None:
    client, proxy, _, request = cut_short(record, *KINDS[kind][:2])
    unread_after_the_first_change(proxy)
    given_up = client.post(f"/requests/{request}/give-up")
    assert (given_up.status_code, UNREAD in given_up.json()["error"], kept_of(record, request).stands) == (503, True, Stands.WORKING)


# What was asked, of which lane, how many changes it made before it was cut short, and the lane once it is finished.
PART_WAY: dict[str, tuple[Asked, Lane, int, Callable[[PretendProxy], bool]]] = {
    "beginning maintenance, with only the back soon page up": (
        BEGIN,
        lane_brought_up_with_a,
        1,
        is_in_maintenance_with_everything_stopped,
    ),
    "beginning maintenance, with the database server still up": (
        BEGIN,
        lane_brought_up_with_a,
        3,
        is_in_maintenance_with_everything_stopped,
    ),
    "ending maintenance, with only the database server started": (END, lane_in_maintenance, 1, is_back_with_everything_up),
    "ending maintenance, with the web part started": (END, lane_in_maintenance, 3, is_back_with_everything_up),
    "a retire, with the route removed": (RETIRE, lane_brought_up_with_a, 1, is_retired),
    "a retire from maintenance, with stopped containers still there": (RETIRE, lane_in_maintenance, 1, is_retired),
}


@pytest.mark.parametrize("part_way", PART_WAY.values(), ids=PART_WAY.keys())
def test_giving_up_a_request_whose_lane_is_part_way_answers_503_changes_nothing_and_the_request_goes_on_and_finishes_it(
    record: Record, part_way: tuple[Asked, Lane, int, Callable[[PretendProxy], bool]]
) -> None:
    asked, lane, changes, is_as_asked = part_way
    client, proxy, worker, request = cut_short(record, asked, lane, changes)
    left = (dict(proxy.written), parts_up(proxy), databases_up(proxy), list(proxy.host.actions))
    given_up = client.post(f"/requests/{request}/give-up")
    assert (given_up.status_code, kept_of(record, request).stands, is_as_asked(proxy)) == (503, Stands.WORKING, False)
    assert "is part way, neither as it was before the request nor as the request would leave it" in given_up.json()["error"]
    assert (dict(proxy.written), parts_up(proxy), databases_up(proxy), list(proxy.host.actions)) == left
    assert (worker.work_next(), kept_of(record, request).stands, is_as_asked(proxy)) == (True, Stands.DONE, True)


def test_giving_up_a_retire_that_has_removed_the_route_and_not_yet_deleted_the_data_set_it_named_is_refused_and_it_deletes_it(
    record: Record,
) -> None:
    deleting = RETIRE.model_copy(update={"delete_data_set": "data-1"})
    client, proxy, worker, request = cut_short(record, deleting, lane_retired, changes=0)
    given_up = client.post(f"/requests/{request}/give-up")
    assert (given_up.status_code, kept_of(record, request).stands, list(proxy.host.data_sets)) == (503, Stands.WORKING, ["data-1"])
    assert (worker.work_next(), kept_of(record, request).stands, list(proxy.host.data_sets)) == (True, Stands.DONE, [])


def test_giving_up_a_move_that_started_its_instance_and_has_not_switched_is_refused_and_the_move_goes_on_and_switches(
    record: Record,
) -> None:
    worker, lanes, proxy = serving_a(record)
    read_again = unread_after_the_first_change(proxy)
    request = record.write(MOVE_B, CALLER, None).id
    assert worker.work_next() is True
    read_again()
    given_up = TestClient(create_app(lanes.read_status, record, worker.wake, NO_SECRETS, lanes)).post(f"/requests/{request}/give-up")
    assert (given_up.status_code, kept_of(record, request).stands, len(proxy.host.instances)) == (503, Stands.WORKING, 2)
    assert (worker.work_next(), (kept_of(record, request).answer or {}).get("outcome")) == (True, "switched")


def test_giving_up_a_move_that_has_made_nothing_ends_it_as_failed_changes_nothing_and_its_late_switch_can_no_longer_rename(
    record: Record,
) -> None:
    worker, lanes, proxy = serving_a(record)
    read_again = unread_after_the_first_change(proxy, changes=0)
    request = record.write(MOVE_B, CALLER, None)
    assert worker.work_next() is True
    read_again()
    given_up = TestClient(create_app(lanes.read_status, record, worker.wake, NO_SECRETS, lanes)).post(
        f"/requests/{request.id}/give-up"
    )
    late = proxy.gate.switch(WEB_ADDRESS, request.generation, 1, "a-late-candidate")
    assert (given_up.status_code, given_up.json()["stands"], "A person gave it up" in given_up.json()["error"]) == (
        200,
        "failed",
        True,
    )
    assert (proxy.host.actions, late.verdict.value, proxy.written, worker.work_next()) == ([], "none", {WEB_ADDRESS: "a"}, False)


LATE_EDITS = {"a retire's removal of the route": (RETIRE, None), "maintenance's back soon page": (BEGIN, BACK_SOON)}


@pytest.mark.parametrize(("asked", "to"), LATE_EDITS.values(), ids=LATE_EDITS.keys())
def test_a_change_of_the_route_sent_before_a_give_up_and_run_after_it_changes_nothing(
    record: Record, asked: Asked, to: str | None
) -> None:
    client, proxy, _, request = cut_short(record, asked, lane_brought_up_with_a, changes=0)
    kept, read = kept_of(record, request), proxy.installed(WEB_ADDRESS)
    given_up = client.post(f"/requests/{request}/give-up")
    late = proxy.edit(WEB_ADDRESS, kept.generation, kept.attempts[-1].id, read, to)
    assert (given_up.status_code, late, proxy.installed(WEB_ADDRESS)) == (200, LOST, read)


# From the moment a give-up is asked, something happens on the host as the lane is read for the given time.
def at_reading(proxy: PretendProxy, number: int, happens: Callable[[], object]) -> None:
    read, readings = proxy.host.read, list[int]()

    def read_as_it_happens(lane: tuple[str, str] | None = None) -> OnHost:
        readings.append(1)
        if len(readings) == number:
            happens()
        return read(lane)

    proxy.host.read = read_as_it_happens


@pytest.mark.parametrize("reading", [1, 2])
def test_a_switch_sent_before_a_give_up_and_run_while_the_lane_is_looked_at_renames_nothing_and_the_give_up_is_refused(
    record: Record, reading: int
) -> None:
    proxy = PretendProxy(PretendHost())
    lanes = Lanes(proxy.host, proxy, name_fresh=numbered_names(), read_unfinished=record.unfinished)
    worker, read_again = Worker(record, lanes, NO_SECRETS), unread_after_the_first_change(proxy)
    request = record.write(MOVE_B, CALLER, None)
    assert worker.work_next() is True
    read_again()
    [candidate], sent_by = proxy.host.instances, kept_of(record, request.id).attempts[-1].id
    at_reading(proxy, reading, lambda: proxy.switch(WEB_ADDRESS, request.generation, sent_by, candidate))
    given_up = TestClient(create_app(lanes.read_status, record, worker.wake, NO_SECRETS, lanes)).post(
        f"/requests/{request.id}/give-up"
    )
    assert (given_up.status_code, proxy.written, kept_of(record, request.id).stands) == (503, {}, Stands.WORKING)


def test_giving_up_a_retire_while_the_logins_of_the_database_it_keeps_cannot_be_read_is_refused_saying_so(record: Record) -> None:
    client, proxy, _, request = cut_short(record, RETIRE, lane_brought_up_with_a, changes=0)
    proxy.host.change_database(DATABASE, unheld_logins=NOT_TOLD)
    given_up = client.post(f"/requests/{request}/give-up")
    assert (given_up.status_code, str(NOT_TOLD) in given_up.json()["error"], kept_of(record, request).stands) == (
        503,
        True,
        Stands.WORKING,
    )


def test_giving_up_a_move_that_repaired_a_background_service_on_a_lane_now_as_asked_ends_it_and_the_service_stays(
    record: Record,
) -> None:
    proxy = lane_whose_background_service_is_gone()
    client, _, worker, request = cut_short(record, MOVE_A, lambda: proxy, changes=2)
    given_up = client.post(f"/requests/{request}/give-up")
    assert (given_up.status_code, len(proxy.host.background_services), worker.work_next()) == (200, 1, False)


def test_giving_up_the_first_move_of_a_lane_that_made_only_the_network_of_its_data_set_is_refused_and_the_move_finishes_it(
    record: Record,
) -> None:
    client, proxy, worker, request = cut_short(record, MOVE_A, lambda: PretendProxy(PretendHost()))
    made = list(proxy.host.actions)
    given_up = client.post(f"/requests/{request}/give-up")
    assert (given_up.status_code, kept_of(record, request).stands, proxy.host.actions, len(made)) == (503, Stands.WORKING, made, 1)
    assert (worker.work_next(), (kept_of(record, request).answer or {}).get("outcome")) == (True, "switched")


def test_giving_up_a_move_that_repaired_a_background_service_while_the_logins_cannot_be_read_is_refused_and_the_service_stays(
    record: Record,
) -> None:
    proxy = lane_whose_background_service_is_gone()
    client, _, _, request = cut_short(record, MOVE_A, lambda: proxy, changes=2)
    proxy.host.change_database(DATABASE, unheld_logins=NOT_TOLD)
    given_up = client.post(f"/requests/{request}/give-up")
    [service] = proxy.host.background_services.values()
    assert (given_up.status_code, kept_of(record, request).stands, service.generation) == (
        503,
        Stands.WORKING,
        kept_of(record, request).generation,
    )


def test_giving_up_a_move_that_made_nothing_on_a_lane_whose_database_has_a_login_no_container_holds_is_refused(
    record: Record,
) -> None:
    client, proxy, _, request = cut_short(record, MOVE_B, lane_brought_up_with_a, changes=0)
    proxy.host.change_database(DATABASE, unheld_logins=("i_left",))
    given_up = client.post(f"/requests/{request}/give-up")
    assert (given_up.status_code, kept_of(record, request).stands, proxy.host.actions) == (503, Stands.WORKING, [])


def stop_the_proxy(proxy: PretendProxy) -> None:
    proxy.running = False


@pytest.mark.parametrize(
    "stops_at_reading", [0, 1, 2], ids=["before the give-up", "as the lane is first read", "as it is read again"]
)
def test_a_give_up_of_a_move_whose_proxy_is_stopped_or_stops_as_the_lane_is_read_is_refused_and_does_not_start_the_proxy(
    record: Record, stops_at_reading: int
) -> None:
    worker, lanes, proxy = serving_a(record)
    read_again = unread_after_the_first_change(proxy)
    request = record.write(MOVE_B, CALLER, None).id
    assert worker.work_next() is True
    read_again()
    made = list(proxy.host.actions)
    at_reading(proxy, stops_at_reading, lambda: stop_the_proxy(proxy))
    if not stops_at_reading:
        stop_the_proxy(proxy)
    given_up = TestClient(create_app(lanes.read_status, record, worker.wake, NO_SECRETS, lanes)).post(f"/requests/{request}/give-up")
    assert (given_up.status_code, proxy.running, proxy.host.actions) == (503, False, made)
    assert ("the proxy is stopped" in given_up.json()["error"]) is (stops_at_reading == 0)


def test_a_cancel_accepted_after_a_give_up_has_read_the_lane_and_before_it_ends_the_request_wins_and_the_give_up_changes_nothing(
    record: Record, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker, lanes, proxy = serving_a(record)
    read_again = unread_after_the_first_change(proxy, changes=0)
    request = record.write(MOVE_B, CALLER, None).id
    assert worker.work_next() is True
    read_again()
    client, end = TestClient(create_app(lanes.read_status, record, worker.wake, NO_SECRETS, lanes)), record.give_up
    cancelled: list[int] = []

    def end_after_a_cancel(ending: int, last: int) -> Request | None:
        cancelled.append(client.post(f"/requests/{ending}/cancel").status_code)
        return end(ending, last)

    monkeypatch.setattr(record, "give_up", end_after_a_cancel)
    given_up = client.post(f"/requests/{request}/give-up")
    again = client.post(f"/requests/{request}/give-up")
    assert (cancelled[0], given_up.status_code, again.status_code, kept_of(record, request).stands) == (
        202,
        503,
        409,
        Stands.WORKING,
    )
    assert (worker.work_next(), kept_of(record, request).stands, client.post(f"/requests/{request}/cancel").status_code) == (
        True,
        Stands.CANCELLED,
        200,
    )


def test_a_retire_taken_up_again_that_finds_its_named_data_set_gone_is_refused_saying_not_known_and_never_nothing_was_deleted(
    record: Record,
) -> None:
    proxy = lane_retired()
    worker = worker_on(proxy, record)
    lose_the_first_reply_to(proxy.host, "delete_data_set")
    request = record.write(RETIRE.model_copy(update={"delete_data_set": "data-1"}), CALLER, None)
    assert (worker.work_next(), left_working(record, request.id).answer, list(proxy.host.data_sets)) == (True, None, [])
    assert worker.work_next() is True
    ended = kept_of(record, request.id)
    assert ended.error is not None
    assert (ended.stands, NOT_KNOWN in ended.error, "Nothing was deleted" in ended.error) == (Stands.FAILED, True, False)


@pytest.mark.parametrize("finished_meanwhile", [False, True], ids=["as it was before the request", "as the request would leave it"])
def test_giving_up_a_request_whose_lane_was_read_and_is_not_part_way_ends_it_as_failed_and_changes_nothing(
    record: Record, kind: str, finished_meanwhile: bool
) -> None:
    client, proxy, worker, request = cut_short(record, *KINDS[kind][:2], changes=0)
    if finished_meanwhile:
        carried_out(lanes_on(proxy), kept_of(record, request), 0, NOBODY)
    left = list(proxy.host.actions)
    given_up = client.post(f"/requests/{request}/give-up")
    assert (given_up.status_code, given_up.json()["stands"], proxy.host.actions) == (200, "failed", left)
    assert (KINDS[kind][5](proxy), worker.work_next()) == (finished_meanwhile, False)


def test_a_retire_that_names_a_data_set_the_lane_does_not_have_and_no_attempt_deleted_fails_naming_it_and_never_says_deleted(
    record: Record,
) -> None:
    proxy = lane_brought_up_with_a()
    request = record.write(RETIRE.model_copy(update={"delete_data_set": "data-l"}), CALLER, None)
    assert worker_on(proxy, record).work_next() is True
    ended = kept_of(record, request.id)
    assert (ended.stands, ended.answer, list(proxy.host.data_sets), is_retired(proxy)) == (Stands.FAILED, None, ["data-1"], True)
    assert "named the data set data-l, which the lane does not have, to delete. Nothing was deleted." in (ended.error or "")


def test_a_readiness_check_docker_would_not_run_while_maintenance_ends_leaves_the_request_working_and_it_ends_once_it_runs(
    record: Record,
) -> None:
    proxy = lane_in_maintenance()
    worker, check = worker_on(proxy, record), proxy.host.await_database

    def restarting_once(container: str) -> None:
        proxy.host.await_database = check
        raise ContainerHostRefused(f"Running pg_isready in {container} on test was refused: the container is restarting")

    proxy.host.await_database = restarting_once
    request = record.write(END, CALLER, None)
    assert worker.work_next() is True
    assert "the container is restarting" in (left_working(record, request.id).error or "")
    assert (worker.work_next(), kept_of(record, request.id).stands, is_back_with_everything_up(proxy)) == (True, Stands.DONE, True)
