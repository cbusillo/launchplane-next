import pytest
from fastapi.testclient import TestClient

from launchplane.api import create_app
from launchplane.docker_answers import ContainerHostError
from launchplane.gate import DONE, LOST
from launchplane.instance import BACK_SOON, InstanceState, OnHost
from launchplane.record import MOST_ATTEMPTS, Record, Stands
from launchplane.steps import MaintenanceOutcome
from launchplane.telling import Witness
from tests.given_secrets import NO_SECRETS
from tests.pretend_host import WEB_ADDRESS, LaunchplaneKilled, PretendProxy
from tests.test_act import lane_brought_up_with_a, lane_in_maintenance, lanes_on
from tests.test_host_answers import BEGIN, END, RETIRE, worker_on
from tests.test_worker import CALLER, kept_of, serving_a

# Every change of a lane's route that is no release: what asks for it, and the lane it is asked of.
EDITS = {
    "a retire": (RETIRE, lane_brought_up_with_a),
    "a maintenance begin": (BEGIN, lane_brought_up_with_a),
    "a maintenance end": (END, lane_in_maintenance),
}
EDITING = pytest.mark.parametrize("asked", EDITS, ids=EDITS.keys())


type Sent = tuple[str, str, int, str, str | None]


# The change is sent, Docker has it, and no reply comes back. Where it has not run by then, it runs when the test lets it.
def accepted_and_not_answered(proxy: PretendProxy, *, has_run: bool) -> list[Sent]:
    edit, held = proxy.edit, list[Sent]()

    def lose_the_reply(web_address: str, generation: str, attempt: int, read: str, container: str | None) -> str:
        held.append((web_address, generation, attempt, read, container))
        proxy.edit = edit
        if has_run:
            edit(*held[0])
        raise ContainerHostError("Running flock in the proxy failed: the connection was lost before the reply")

    proxy.edit = lose_the_reply
    return held


ENDS_AS = {"a retire": "retired", "a maintenance begin": "in maintenance", "a maintenance end": "not in maintenance"}


@EDITING
@pytest.mark.parametrize("has_run", [True, False], ids=["it had been made", "it had not run"])
def test_a_change_of_the_route_whose_reply_is_lost_never_fails_its_request_half_way_the_next_attempt_fences_it_reads_and_goes_on(
    record: Record, asked: str, has_run: bool
) -> None:
    work, lane = EDITS[asked]
    proxy = lane()
    worker, edit = worker_on(proxy, record), proxy.edit
    held = accepted_and_not_answered(proxy, has_run=has_run)
    request = record.write(work, CALLER, None)
    assert worker.work_next() is True
    left = kept_of(record, request.id)
    assert (left.stands, [each.ended for each in left.attempts]) == (Stands.WORKING, ["unfinished"])
    assert worker.work_next() is True
    ended = kept_of(record, request.id)
    outcome = ENDS_AS[asked] if has_run or work is not END else "ended"
    assert (ended.stands, (ended.answer or {})["outcome"], edit(*held[0]) != DONE) == (Stands.DONE, outcome, True)
    running = {part.state for part in (*proxy.host.instances.values(), *proxy.host.background_services.values())}
    assert work is not BEGIN or (proxy.written[WEB_ADDRESS], running) == (BACK_SOON, {InstanceState.STOPPED})


def test_a_request_whose_changes_of_the_route_were_all_answered_and_one_that_asked_for_none_ask_the_gate_for_no_fence(
    record: Record,
) -> None:
    proxy = lane_brought_up_with_a()
    worker = worker_on(proxy, record)
    for work in (BEGIN, END, RETIRE, RETIRE, END):
        request = record.write(work, CALLER, None)
        assert (worker.work_next(), kept_of(record, request.id).stands) == (True, Stands.DONE)
    assert {kept.fenced for kept in proxy.gate.kept.values()} == {-1}


def test_a_request_found_working_whose_fence_gets_no_answer_does_nothing_on_the_host_keeps_its_lane_and_goes_on_once_it_does(
    record: Record,
) -> None:
    proxy = lane_brought_up_with_a()
    worker = worker_on(proxy, record)
    retire = record.write(RETIRE, CALLER, None)
    assert record.take_up(retire.id) is not None
    proxy.gate.unreadable = "The proxy's container did not answer"
    assert worker.work_next() is True
    left = kept_of(record, retire.id)
    assert (left.stands, [each.ended for each in left.attempts], proxy.host.actions) == (
        Stands.WORKING,
        ["interrupted", "unfinished"],
        [],
    )
    assert left.error == "The proxy's container did not answer"
    proxy.gate.unreadable = None
    assert worker.work_next() is True
    assert (kept_of(record, retire.id).stands, WEB_ADDRESS in proxy.written) == (Stands.DONE, False)


def test_a_request_listed_as_waiting_and_since_taken_up_by_a_worker_that_died_is_fenced_from_its_attempts_as_they_are_now(
    record: Record,
) -> None:
    proxy = lane_brought_up_with_a()
    request = record.write(BEGIN, CALLER, None)
    [listed_before] = record.next_up()
    died_in = record.take_up(request.id)
    assert (listed_before.stands, died_in is not None) == (Stands.WAITING, True)
    record.cancel(request.id, CALLER)
    worker_on(proxy, record).worked(listed_before)
    late = proxy.edit(WEB_ADDRESS, request.generation, died_in or 0, proxy.installed(WEB_ADDRESS), BACK_SOON)
    assert (kept_of(record, request.id).stands, late, proxy.written[WEB_ADDRESS] != BACK_SOON) == (Stands.CANCELLED, LOST, True)


def test_a_request_left_to_a_person_at_the_limit_of_interruptions_is_fenced_through_its_last_attempt_first(record: Record) -> None:
    proxy = lane_brought_up_with_a()
    request = record.write(BEGIN, CALLER, None)
    attempts = [record.take_up(request.id) for _ in range(MOST_ATTEMPTS)]
    assert worker_on(proxy, record).work_next() is True
    late = proxy.edit(WEB_ADDRESS, request.generation, attempts[-1] or 0, proxy.installed(WEB_ADDRESS), BACK_SOON)
    assert (kept_of(record, request.id).stands, late, proxy.written[WEB_ADDRESS] != BACK_SOON) == (Stands.NEEDS_A_PERSON, LOST, True)


def test_a_request_found_working_fences_its_earlier_attempts_before_it_reads_its_lane_so_no_change_of_theirs_lands_under_it(
    record: Record,
) -> None:
    proxy = lane_in_maintenance()
    worker, edit, read_host = worker_on(proxy, record), proxy.edit, proxy.host.read
    sent: list[Sent] = []
    arrived: list[str] = []

    def killed_once_sent(web_address: str, generation: str, attempt: int, read: str, container: str | None) -> str:
        sent.append((web_address, generation, attempt, read, container))
        raise LaunchplaneKilled

    def read_once_the_old_change_has_arrived(lane: tuple[str, str] | None = None) -> OnHost:
        arrived.append(edit(*sent[0]))
        return read_host(lane)

    proxy.edit = killed_once_sent
    request = record.write(END, CALLER, None)
    with pytest.raises(LaunchplaneKilled):
        worker.work_next()
    proxy.edit, proxy.host.read = edit, read_once_the_old_change_has_arrived
    record.cancel(request.id, CALLER)
    assert worker.work_next() is True
    assert (set(arrived), kept_of(record, request.id).stands, proxy.written[WEB_ADDRESS]) == ({LOST}, Stands.CANCELLED, BACK_SOON)


def test_giving_up_a_retire_that_needs_a_person_fences_every_attempt_of_it_before_its_lane_takes_another_request(
    record: Record,
) -> None:
    worker, lanes, proxy = serving_a(record)
    client = TestClient(create_app(lanes.read_status, record, worker.wake, NO_SECRETS, lanes))
    asked = client.post("/lanes/site/testing/retire", json={}).json()
    attempts = [record.take_up(asked["id"]) for _ in range(MOST_ATTEMPTS)]
    assert (worker.work_next(), kept_of(record, asked["id"]).stands) == (True, Stands.NEEDS_A_PERSON)
    proxy.gate.unreadable = "The proxy's container did not answer"
    unasked = [client.post(f"/requests/{asked['id']}/{call}").status_code for call in ("retry", "give-up")]
    proxy.gate.unreadable = None
    assert (unasked, kept_of(record, asked["id"]).stands) == ([503, 503], Stands.NEEDS_A_PERSON)
    assert client.post(f"/requests/{asked['id']}/give-up").status_code == 200
    late = [proxy.edit(WEB_ADDRESS, asked["generation"], attempt or 0, "a", None) for attempt in attempts]
    assert (late, proxy.written[WEB_ADDRESS]) == ([LOST] * MOST_ATTEMPTS, "a")


def test_a_change_the_gate_did_not_make_is_told_as_refused_and_the_lane_is_read_again_and_decided_again() -> None:
    proxy = lane_brought_up_with_a()
    edit = proxy.edit
    told: list[tuple[str, str | None]] = []

    def not_the_route_read_once(web_address: str, generation: str, attempt: int, read: str, container: str | None) -> str:
        proxy.edit = edit
        return edit(web_address, generation, attempt, f"{read}, as it no longer is", container)

    proxy.edit = not_the_route_read_once
    answer = lanes_on(proxy).begin_maintenance("site", "testing", Witness(tell=lambda did, error: told.append((did, error))))
    switch = f"send {WEB_ADDRESS} to {BACK_SOON}"
    assert (answer.outcome, told[:2]) == (
        MaintenanceOutcome.IN_MAINTENANCE,
        [(switch, "The gate left the route: it was not the one read."), (switch, None)],
    )


def test_a_retire_whose_removal_the_gate_never_makes_retires_no_container_and_stays_working_saying_so(record: Record) -> None:
    proxy = lane_brought_up_with_a()

    def never_made(web_address: str, generation: str, attempt: int, read: str, container: str | None) -> str:
        assert web_address and generation and attempt and read and container is None
        return LOST

    proxy.edit = never_made
    request = record.write(RETIRE, CALLER, None)
    assert worker_on(proxy, record).work_next() is True
    kept = kept_of(record, request.id)
    assert (kept.stands, WEB_ADDRESS in proxy.written, len(proxy.host.instances)) == (Stands.WORKING, True, 1)
    assert "was not finished after 50 steps" in (kept.error or "")


def test_beginning_maintenance_refused_before_any_change_of_the_route_was_sent_fails_with_why_and_its_lane_takes_the_next_request(
    record: Record,
) -> None:
    proxy = lane_brought_up_with_a()
    proxy.host.refuses_helpers = True
    worker = worker_on(proxy, record)
    begin = record.write(BEGIN, CALLER, None)
    assert worker.work_next() is True
    retire = record.write(RETIRE, CALLER, None)
    assert worker.work_next() is True
    refused = kept_of(record, begin.id)
    assert (refused.stands, "pull access denied" in (refused.error or ""), kept_of(record, retire.id).stands) == (
        Stands.FAILED,
        True,
        Stands.DONE,
    )
