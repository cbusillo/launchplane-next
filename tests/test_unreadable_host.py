from collections.abc import Callable
from typing import NoReturn

import pytest
from fastapi.testclient import TestClient

from launchplane.api import create_app
from launchplane.docker_answers import ContainerHostError, ContainerHostRefused
from launchplane.instance import BACK_SOON, InstanceState, OnHost
from launchplane.record import MOST_ATTEMPTS, Asked, Kept, Record, Stands
from launchplane.worker import Worker
from tests.given_secrets import NO_SECRETS
from tests.pretend_host import WEB_ADDRESS, PretendProxy
from tests.test_act import lane_brought_up_with_a, lane_in_maintenance, lanes_on
from tests.test_host_answers import BEGIN, END, MOVE_A, RETIRE, worker_on
from tests.test_worker import CALLER, kept_of


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


# What is asked, the lane it is asked of, a change it sends that is no change of the route, its first change of all,
# the lane on which nothing is left for it to do, and what the host shows once it is done.
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

    def made_and_not_answered(container: str) -> None:
        setattr(host, change, made)
        made(container)
        raise ContainerHostError(f"{change} of {container} on test failed: the connection was lost before the reply")

    setattr(host, change, made_and_not_answered)


def refuse(*_sent: object) -> NoReturn:
    raise ContainerHostRefused("The change on test was refused: pull access denied")


def fence_refused(web_address: str, generation: str, attempt: int) -> NoReturn:
    refuse(web_address, generation, attempt)


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
    for _ in range(MOST_ATTEMPTS):
        assert worker.work_next() is True
    left = left_working(record, request.id)
    assert (left.error, len(left.attempts), kept_of(record, behind.id).stands) == (UNREAD, MOST_ATTEMPTS + 1, Stands.WAITING)
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


def test_a_request_found_working_whose_fence_the_host_refuses_to_run_does_nothing_and_stays_working_until_the_gate_answers(
    record: Record, kind: str
) -> None:
    asked, lane, _, _, _, is_as_asked, _ = KINDS[kind]
    proxy = lane()
    worker, fence = worker_on(proxy, record), proxy.gate.fence
    request = record.write(asked, CALLER, None)
    assert record.take_up(request.id) is not None
    proxy.gate.fence = fence_refused
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


# Ending maintenance starts the database server, waits for it, and only then starts a part.
CHANGES_UNTIL_A_PART_IS_UP = {"a maintenance end": 3}


# The lane as a request of this kind cut short leaves it, with a part up and that request needing a person.
def cut_short_and_left_to_a_person(record: Record, kind: str) -> tuple[TestClient, PretendProxy, Worker, int]:
    asked, lane, _, _, _, _, _ = KINDS[kind]
    proxy = lane()
    lanes = lanes_on(proxy)
    worker = Worker(record, lanes, NO_SECRETS)
    read_again = unread_after_the_first_change(proxy, CHANGES_UNTIL_A_PART_IS_UP.get(kind, 1))
    request = record.write(asked, CALLER, None)
    assert worker.work_next() is True
    read_again()
    for _ in range(MOST_ATTEMPTS):
        assert record.take_up(request.id) is not None
    assert (worker.work_next(), kept_of(record, request.id).stands) == (True, Stands.NEEDS_A_PERSON)
    return TestClient(create_app(lanes.read_status, record, worker.wake, NO_SECRETS, lanes)), proxy, worker, request.id


def test_giving_up_a_request_whose_lane_cannot_be_read_answers_503_and_the_request_still_needs_a_person(
    record: Record, kind: str
) -> None:
    client, proxy, _, request = cut_short_and_left_to_a_person(record, kind)
    unread_after_the_first_change(proxy)
    given_up = client.post(f"/requests/{request}/give-up")
    assert (given_up.status_code, UNREAD in given_up.json()["error"], kept_of(record, request).stands) == (
        503,
        True,
        Stands.NEEDS_A_PERSON,
    )


def test_giving_up_a_request_whose_lane_is_part_way_is_refused_saying_what_is_still_up_and_asking_again_finishes_the_lane(
    record: Record, kind: str
) -> None:
    client, proxy, worker, request = cut_short_and_left_to_a_person(record, kind)
    is_as_asked = KINDS[kind][5]
    up = parts_up(proxy)
    given_up = client.post(f"/requests/{request}/give-up")
    assert (given_up.status_code, kept_of(record, request).stands, bool(up)) == (409, Stands.NEEDS_A_PERSON, True)
    assert f"{WEB_ADDRESS} sends visitors to no instance and {', '.join(up)} is still up" in given_up.json()["error"]
    again = client.post(f"/requests/{request}/retry").json()["id"]
    assert (worker.work_next(), kept_of(record, again).stands, is_as_asked(proxy)) == (True, Stands.DONE, True)


def test_giving_up_a_request_whose_lane_was_read_and_is_not_part_way_ends_it_and_changes_nothing(record: Record, kind: str) -> None:
    asked, lane, _, _, _, _, _ = KINDS[kind]
    proxy = lane()
    lanes = lanes_on(proxy)
    worker = Worker(record, lanes, NO_SECRETS)
    request = record.write(asked, CALLER, None)
    for _ in range(MOST_ATTEMPTS):
        assert record.take_up(request.id) is not None
    assert worker.work_next() is True
    client = TestClient(create_app(lanes.read_status, record, worker.wake, NO_SECRETS, lanes))
    given_up = client.post(f"/requests/{request.id}/give-up")
    assert (given_up.status_code, given_up.json()["stands"], proxy.host.actions) == (200, "failed", [])
