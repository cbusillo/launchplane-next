import threading
from collections.abc import Iterator
from dataclasses import dataclass

import pytest
from fastapi.testclient import TestClient

from launchplane.act import Lanes
from launchplane.api import create_app
from launchplane.instance import LaneBuild
from launchplane.record import MOST_ATTEMPTS, Record
from launchplane.worker import Worker
from tests.given_secrets import NO_SECRETS, GivenSecrets
from tests.pretend_host import WEB_ADDRESS, PretendHost, PretendProxy, numbered_names
from tests.pretend_record import PretendRecord
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


def bed_on(record: Record) -> Bed:
    proxy = PretendProxy(PretendHost())
    proxy.host.place(LaneBuild(product="site", lane="testing", build="site:a"), "a")
    proxy.route(WEB_ADDRESS, "a")
    lanes = Lanes(proxy.host, proxy, name_fresh=numbered_names(), read_unfinished=record.unfinished)
    worker = Worker(record, lanes, NO_SECRETS)
    return Bed(TestClient(create_app(lanes.read_status, record, worker.wake, NO_SECRETS)), proxy, record, worker)


@pytest.fixture
def bed(record: Record) -> Iterator[Bed]:
    made = bed_on(record)
    thread = made.worker.start()
    yield made
    made.worker.stop()
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


def test_a_request_that_needs_a_person_stops_its_lane_until_it_is_asked_for_again_or_given_up(idle: Bed) -> None:
    stuck = idle.client.post(MOVE, json=TO_B).json()["id"]
    for _ in range(MOST_ATTEMPTS):
        idle.record.take_up(stuck)
    assert idle.worker.worked_one() is True
    refused = idle.client.post("/lanes/site/testing/retire", json={})
    again = idle.client.post(f"/requests/{stuck}/retry")
    twice = idle.client.post(f"/requests/{stuck}/retry")
    assert (refused.status_code, f"POST /requests/{stuck}/retry" in refused.json()["error"]) == (409, True)
    assert (again.status_code, again.headers["location"], again.json()["stands"]) == (
        202,
        f"/requests/{again.json()['id']}",
        "waiting",
    )
    assert (twice.status_code, "is failed" in twice.json()["error"]) == (409, True)
    assert idle.worker.worked_one() is True
    assert idle.client.get(again.headers["location"]).json()["answer"]["outcome"] == "switched"


def test_giving_up_a_request_that_needs_a_person_ends_it_and_changes_nothing_on_the_host(idle: Bed) -> None:
    stuck = idle.client.post(MOVE, json=TO_B).json()["id"]
    for _ in range(MOST_ATTEMPTS):
        idle.record.take_up(stuck)
    idle.worker.worked_one()
    given_up = idle.client.post(f"/requests/{stuck}/give-up")
    assert (given_up.status_code, given_up.json()["stands"], idle.proxy.host.actions) == (200, "failed", [])
    assert idle.client.post("/lanes/site/testing/retire", json={}).status_code == 202


def test_a_request_that_is_not_there_is_said_not_to_be(idle: Bed) -> None:
    answers = [idle.client.get("/requests/7"), idle.client.post("/requests/7/retry"), idle.client.post("/requests/7/give-up")]
    assert [each.status_code for each in answers] == [404, 404, 404]


def test_with_the_record_unreachable_a_call_that_changes_something_is_refused_and_changes_nothing() -> None:
    bed = bed_on(PretendRecord(reachable=False))
    calls = [
        bed.client.post(MOVE, json=TO_B),
        bed.client.post("/lanes/site/testing/retire", json={}),
        bed.client.post("/lanes/site/testing/maintenance/begin"),
        bed.client.post("/lanes/site/testing/maintenance/end"),
        bed.client.post("/requests/1/retry"),
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
    return TestClient(create_app(lanes.read_status, record, lambda: None, secrets))


def test_a_secret_sent_as_the_key_is_not_shown_once_its_container_is_gone_and_launchplane_has_restarted(record: Record) -> None:
    key = {"Idempotency-Key": SECRET}
    while_held = app_on(record, GivenSecrets(on_host=frozenset({SECRET})))
    first = while_held.post(MOVE, json=TO_B, headers=key)
    after_a_restart = app_on(record, GivenSecrets())
    shown = after_a_restart.get(first.headers["location"]).text + after_a_restart.get("/requests").text
    again = after_a_restart.post(MOVE, json=TO_B, headers=key)
    assert (first.status_code, SECRET in shown, again.headers["location"]) == (202, False, first.headers["location"])


SECRETS = {
    "of letters, digits and marks": SECRET,
    "with a quote, a backslash and a newline": 'Zx3"quoted\\slashed\nand-on-two-lines',
}


@pytest.mark.parametrize("secret", SECRETS.values(), ids=SECRETS.keys())
def test_a_call_that_carries_a_secret_a_container_holds_is_refused_and_nothing_of_it_is_written(record: Record, secret: str) -> None:
    client = app_on(record, GivenSecrets(on_host=frozenset({secret})))
    refused = [
        client.post(MOVE, json={"build": f"registry.test/site:{secret}"}),
        client.post("/lanes/site/testing/retire", json={"delete_data_set": secret}),
    ]
    assert [each.status_code for each in refused] == [422, 422]
    assert all("was not written" in each.json()["error"] for each in refused)
    assert (record.unfinished(), record.recently_ended()) == ([], [])


def test_an_answer_says_nothing_was_changed_only_when_the_request_is_known_not_to_be_written() -> None:
    unreachable = bed_on(PretendRecord(reachable=False)).client.post(MOVE, json=TO_B)
    unconfirmed_record = PretendRecord(confirms_writes=False)
    unconfirmed = bed_on(unconfirmed_record).client.post(MOVE, json=TO_B, headers={"Idempotency-Key": "one-release"})
    assert (unreachable.status_code, "was not written and nothing was changed" in unreachable.json()["error"]) == (503, True)
    assert (unconfirmed.status_code, "nothing was changed" in unconfirmed.json()["error"]) == (503, False)
    assert "is not known" in unconfirmed.json()["error"] and "GET /requests" in unconfirmed.json()["error"]
    assert len(unconfirmed_record.unfinished()) == 1
