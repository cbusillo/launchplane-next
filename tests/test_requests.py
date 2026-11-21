import json
import socket
import threading
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass

import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.testclient import TestClient

from launchplane.act import Lanes
from launchplane.api import create_app, server_config
from launchplane.instance import LaneBuild, Verdict
from launchplane.record import Record, Stands
from launchplane.worker import Worker
from tests.given_secrets import NO_SECRETS, GivenSecrets
from tests.pretend_host import WEB_ADDRESS, PretendHost, PretendProxy, numbered_names
from tests.pretend_record import PretendRecord, begun
from tests.test_worker import wait_until

MOVE = "/lanes/site/testing/move"
TO_B = {"build": "site:b"}
SECRET = "Zx3-secret_given-to-a-lane-0123456789abcdef"  # noqa: S105 a pretend one


@dataclass
class Bed:
    client: TestClient
    proxy: PretendProxy
    record: Record
    worker: Worker
    app: FastAPI


def bed_on(record: Record) -> Bed:
    proxy = PretendProxy(PretendHost())
    proxy.host.place(LaneBuild(product="site", lane="testing", build="site:a"), "a")
    proxy.route(WEB_ADDRESS, "a")
    lanes = Lanes(proxy.host, proxy, name_fresh=numbered_names(), read_unfinished=record.unfinished)
    worker = Worker(record, lanes, NO_SECRETS)
    lanes.read_cancels = record.cancels_answered
    app = create_app(lanes.read_status, record, worker.wake, NO_SECRETS, lanes)
    return Bed(TestClient(app), proxy, record, worker, app)


@pytest.fixture
def bed(record: Record) -> Iterator[Bed]:
    made = bed_on(record)
    threads = made.worker.start()
    yield made
    made.worker.stop()
    for thread in threads:
        thread.join(timeout=5)


@pytest.fixture
def idle(record: Record) -> Bed:
    return bed_on(record)


def test_a_call_is_answered_at_once_with_where_to_read_its_request_and_the_request_ends_by_itself(bed: Bed) -> None:
    checking, checked = threading.Event(), threading.Event()

    def hold_the_check(_container: str) -> None:
        checking.set()
        assert checked.wait(timeout=5)

    bed.proxy.while_checking = hold_the_check
    asked = bed.client.post(MOVE, json=TO_B)
    where = asked.headers["location"]
    assert checking.wait(timeout=5)
    meanwhile = bed.client.get(where).json()
    asked_again = bed.client.post(MOVE, json=TO_B)
    checked.set()
    wait_until(lambda: bed.client.get(where).json()["stands"] == "done")
    ended = bed.client.get(where).json()
    assert (asked.status_code, where, asked_again.headers["location"]) == (202, f"/requests/{asked.json()['id']}", where)
    assert (meanwhile["stands"], [event["did"] for event in meanwhile["events"]]) == (
        "working",
        ["start the instance site-testing-0 of site:b"],
    )
    assert (ended["answer"]["outcome"], ended["asked_by"], len(ended["attempts"])) == ("switched", "the address testclient", 1)
    assert set(ended["attempts"][0]) == {"number", "began_at", "ended_at", "ended"}


def test_a_call_that_waits_answers_as_it_did_before_requests_were_kept(bed: Bed) -> None:
    switched = bed.client.post(f"{MOVE}?wait=5", json=TO_B)
    unchanged = bed.client.post(f"{MOVE}?wait=5", json=TO_B)
    bed.proxy.while_checking = lambda container: bed.proxy.failing.add(container)
    check_failed = bed.client.post(f"{MOVE}?wait=5", json={"build": "site:c"})
    retired = bed.client.post("/lanes/site/testing/retire?wait=5", json={})
    nothing_there = bed.client.post("/lanes/site/testing/maintenance/begin?wait=5")
    assert [(each.status_code, each.json().get("outcome")) for each in (switched, unchanged, check_failed, retired)] == [
        (200, "switched"),
        (200, "unchanged"),
        (422, "check failed"),
        (200, "retired"),
    ]
    assert (nothing_there.status_code, "stopped, and is safe to ask again" in nothing_there.json()["error"]) == (503, True)


def test_a_call_whose_request_does_not_end_within_the_wait_answers_accepted(idle: Bed) -> None:
    waited = idle.client.post(f"{MOVE}?wait=0.2", json=TO_B)
    too_long = idle.client.post(f"{MOVE}?wait=3601", json=TO_B)
    assert (waited.status_code, waited.json()["stands"], too_long.status_code) == (202, "waiting", 422)


def test_the_same_key_answers_with_the_request_already_written_and_with_another_body_is_refused(bed: Bed) -> None:
    key = {"Idempotency-Key": "one-release"}
    first = bed.client.post(f"{MOVE}?wait=5", json=TO_B, headers=key)
    again = bed.client.post(f"{MOVE}?wait=5", json=TO_B, headers=key)
    other = bed.client.post(f"{MOVE}?wait=5", json={"build": "site:c"}, headers=key)
    unwaited = bed.client.post(MOVE, json=TO_B, headers=key)
    listed = bed.client.get("/requests").json()
    assert (first.json(), first.headers["location"]) == (again.json(), again.headers["location"])
    assert (unwaited.status_code, unwaited.json()["stands"], unwaited.json()["answer"]) == (202, "done", first.json())
    assert (other.status_code, "asked something else" in other.json()["error"]) == (409, True)
    assert ([each["id"] for each in listed["ended"]], listed["unfinished"], bed.proxy.host.actions.count("retire a")) == ([1], [], 1)


def test_a_lane_whose_request_was_interrupted_four_times_is_not_locked_and_a_new_request_waits_behind_it_and_is_worked_next(
    idle: Bed,
) -> None:
    interrupted = idle.client.post(MOVE, json=TO_B).json()["id"]
    for _ in range(4):
        assert idle.record.take_up(interrupted) is not None
    behind = idle.client.post("/lanes/site/testing/retire", json={})
    assert (behind.status_code, behind.json()["stands"]) == (202, "waiting")
    assert (idle.worker.worked_one(), idle.worker.worked_one()) == (True, True)
    ended = [idle.client.get(f"/requests/{each}").json() for each in (interrupted, behind.json()["id"])]
    assert [(each["stands"], each["answer"]["outcome"]) for each in ended] == [("done", "switched"), ("done", "retired")]
    assert [attempt["ended"] for attempt in ended[0]["attempts"]] == [*["interrupted"] * 4, "done"]


def test_a_replaced_request_says_by_which_to_the_call_that_waited_in_the_list_and_on_the_page_and_is_not_shown_as_wrong(
    idle: Bed,
) -> None:
    at_work = idle.client.post(MOVE, json=TO_B).json()["id"]
    assert idle.record.take_up(at_work) is not None
    to_c, key = {"build": "site:c"}, {"Idempotency-Key": "c"}
    waited = idle.client.post(MOVE, json=to_c, headers=key).json()["id"]
    newest = idle.client.post(MOVE, json={"build": "site:d"}).json()["id"]
    told = f"The move of testing of site to site:c was replaced by request {newest} while it waited."
    asked_and_waited = idle.client.post(MOVE, json=to_c, headers=key, params={"wait": 5})
    [ended] = idle.client.get("/requests").json()["ended"]
    [card] = idle.client.get("/running").json()["cards"]
    [row] = [row for row in card["rows"] if row["name"] == f"Request {waited}"]
    cancel = idle.client.post(f"/requests/{waited}/cancel", params={"wait": 5})
    assert (ended["id"], ended["stands"], ended["replaced_by"], row["pills"]) == (
        waited,
        "replaced",
        newest,
        [{"text": "replaced", "tone": "on purpose"}],
    )
    assert (row["note"].startswith(told), cancel.status_code, "ended as replaced" in cancel.json()["error"]) == (True, 409, True)
    assert (asked_and_waited.status_code, asked_and_waited.json()["error"].startswith(told)) == (409, True)


def test_giving_up_a_request_that_waits_or_is_being_worked_at_that_moment_or_has_ended_is_refused_and_changes_nothing(
    idle: Bed,
) -> None:
    asked = idle.client.post(MOVE, json=TO_B).json()["id"]
    waiting = idle.client.post(f"/requests/{asked}/give-up")
    attempt = begun(idle.record, asked)
    being_worked = idle.client.post(f"/requests/{asked}/give-up")
    how_it_stood = idle.client.get(f"/requests/{asked}").json()["stands"]
    idle.record.end(attempt, Stands.DONE, {"outcome": "switched"}, None, decided=Verdict.COMMITTED)
    ended = idle.client.post(f"/requests/{asked}/give-up")
    assert [each.status_code for each in (waiting, being_worked, ended)] == [409, 503, 409]
    assert (
        "it is waiting" in waiting.json()["error"],
        "being worked at this moment" in being_worked.json()["error"],
    ) == (
        True,
        True,
    )
    assert (how_it_stood, idle.client.get(f"/requests/{asked}").json()["stands"], idle.proxy.host.actions) == ("working", "done", [])


def test_a_request_that_is_not_there_is_said_not_to_be(idle: Bed) -> None:
    answers = [idle.client.get("/requests/7"), idle.client.post("/requests/7/give-up"), idle.client.post("/requests/7/cancel")]
    assert [each.status_code for each in answers] == [404, 404, 404]


def test_with_the_record_unreachable_a_call_that_changes_something_is_refused_and_changes_nothing() -> None:
    bed = bed_on(PretendRecord(reachable=False))
    calls = [
        bed.client.post(MOVE, json=TO_B),
        bed.client.post("/lanes/site/testing/retire", json={}),
        bed.client.post("/lanes/site/testing/maintenance/begin"),
        bed.client.post("/lanes/site/testing/maintenance/end"),
        bed.client.post("/requests/1/give-up"),
        bed.client.get("/requests"),
        bed.client.get("/requests/1"),
    ]
    running = bed.client.get("/running")
    assert [each.status_code for each in calls] == [503] * len(calls)
    assert all("could not be reached" in each.json()["error"] for each in calls)
    assert (bed.worker.worked_one(), bed.proxy.host.actions) == (False, [])
    assert (running.status_code, len(running.json()["instances"]), "could not be reached" in running.json()["record_error"]) == (
        200,
        1,
        True,
    )


def app_on(record: Record, secrets: GivenSecrets) -> TestClient:
    proxy = PretendProxy(PretendHost())
    lanes = Lanes(proxy.host, proxy, read_unfinished=record.unfinished)
    return TestClient(create_app(lanes.read_status, record, lambda: None, secrets, lanes))


def test_a_secret_sent_as_the_key_is_not_shown_once_its_container_is_gone_and_launchplane_has_restarted(
    record: Record, secrets: GivenSecrets
) -> None:
    key = {"Idempotency-Key": SECRET}
    secrets.on_host = frozenset({SECRET})
    first = app_on(record, secrets).post(MOVE, json=TO_B, headers=key)
    secrets.on_host = secrets.seen = frozenset()
    after_a_restart = app_on(record, secrets)
    shown = after_a_restart.get(first.headers["location"]).text + after_a_restart.get("/requests").text
    again = after_a_restart.post(MOVE, json=TO_B, headers=key)
    assert (first.status_code, SECRET in shown, again.headers["location"]) == (202, False, first.headers["location"])


SECRETS = {
    "of letters, digits and marks": SECRET,
    "with a quote, a backslash and a newline": 'Zx3"quoted\\slashed\nand-on-two-lines',
}


@pytest.mark.parametrize("secret", SECRETS.values(), ids=SECRETS.keys())
def test_a_call_that_carries_a_secret_a_container_holds_is_refused_and_nothing_of_it_is_written(
    record: Record, secrets: GivenSecrets, secret: str
) -> None:
    secrets.on_host = frozenset({secret})
    client = app_on(record, secrets)
    refused = [
        client.post(MOVE, json={"build": f"registry.test/site:{secret}"}),
        client.post("/lanes/site/testing/retire", json={"delete_data_set": secret}),
    ]
    assert [each.status_code for each in refused] == [422, 422]
    assert all("nothing was written" in each.json()["error"] for each in refused)
    assert (record.unfinished(), record.recently_ended()) == ([], [])


def test_a_call_the_record_could_not_be_asked_about_is_not_told_nothing_was_changed_with_a_key_or_without() -> None:
    unconfirmed_record = PretendRecord(confirms_writes=False)
    unconfirmed = bed_on(unconfirmed_record).client.post(MOVE, json=TO_B, headers={"Idempotency-Key": "one-release"})
    for key in ({}, {"Idempotency-Key": "one-release"}):
        repeated = bed_on(PretendRecord(reachable=False)).client.post(MOVE, json=TO_B, headers=key)
        assert (repeated.status_code, "nothing was changed" in repeated.json()["error"]) == (503, False)
        assert "is not known" in repeated.json()["error"]
    assert (unconfirmed.status_code, "nothing was changed" in unconfirmed.json()["error"]) == (503, False)
    assert "is not known" in unconfirmed.json()["error"] and "GET /requests" in unconfirmed.json()["error"]
    assert len(unconfirmed_record.unfinished()) == 1


def test_who_asked_is_the_address_the_connection_came_from_whatever_a_forwarding_header_says() -> None:
    bed = bed_on(PretendRecord())
    with socket.socket() as free:
        free.bind(("127.0.0.1", 0))
        port = free.getsockname()[1]
    server = uvicorn.Server(server_config(bed.app, "127.0.0.1", port, NO_SECRETS))
    thread = threading.Thread(target=server.run)
    thread.start()
    try:
        wait_until(lambda: server.started)
        asked = urllib.request.Request(
            f"http://127.0.0.1:{port}{MOVE}",
            data=json.dumps(TO_B).encode(),
            headers={"Content-Type": "application/json", "X-Forwarded-For": "a-caller-chose-this-text"},
        )
        with urllib.request.urlopen(asked, timeout=5) as answer:  # noqa: S310
            written = json.loads(answer.read())
    finally:
        server.should_exit = True
        thread.join(timeout=10)
    assert written["asked_by"] == "the address 127.0.0.1"
