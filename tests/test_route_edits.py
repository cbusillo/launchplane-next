import pytest
from fastapi.testclient import TestClient

from launchplane.api import create_app
from launchplane.docker_answers import ContainerHostError
from launchplane.gate import LOST
from launchplane.gate import Answer as GateAnswer
from launchplane.instance import BACK_SOON
from launchplane.record import MOST_ATTEMPTS, Record, Stands
from launchplane.steps import MaintenanceOutcome
from launchplane.telling import Witness
from tests.given_secrets import NO_SECRETS
from tests.pretend_host import WEB_ADDRESS, PretendHost, PretendProxy
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


# The change is sent, Docker has it, and no reply comes back. It runs when the test lets it.
def accepted_and_not_answered(proxy: PretendProxy) -> list[Sent]:
    held: list[Sent] = []

    def lose_the_reply(web_address: str, generation: str, attempt: int, read: str, container: str | None) -> str:
        held.append((web_address, generation, attempt, read, container))
        raise ContainerHostError("Running flock in the proxy failed: the connection was lost before the reply")

    proxy.edit = lose_the_reply
    return held


@EDITING
def test_a_request_that_asked_for_a_change_of_the_route_is_fenced_through_that_attempt_while_it_still_holds_its_lane(
    record: Record, asked: str
) -> None:
    work, lane = EDITS[asked]
    proxy = lane()
    edit, fence = proxy.edit, proxy.gate.fence
    sent: list[int] = []
    fenced: list[tuple[int, Stands]] = []

    def edit_and_note(web_address: str, generation: str, attempt: int, read: str, container: str | None) -> str:
        sent.append(attempt)
        return edit(web_address, generation, attempt, read, container)

    def fence_and_note(web_address: str, generation: str, attempt: int) -> GateAnswer:
        fenced.append((attempt, kept_of(record, request.id).stands))
        return fence(web_address, generation, attempt)

    proxy.edit, proxy.gate.fence = edit_and_note, fence_and_note
    request = record.write(work, CALLER, None)
    assert worker_on(proxy, record).work_next() is True
    assert (kept_of(record, request.id).stands, fenced) == (Stands.DONE, [(sent[0], Stands.WORKING)])


@EDITING
def test_a_change_accepted_and_not_answered_fails_its_request_and_arriving_late_changes_no_route_also_not_one_in_maintenance(
    record: Record, asked: str
) -> None:
    work, lane = EDITS[asked]
    proxy = lane()
    worker, edit, before = worker_on(proxy, record), proxy.edit, proxy.written[WEB_ADDRESS]
    held = accepted_and_not_answered(proxy)
    request = record.write(work, CALLER, None)
    assert worker.work_next() is True
    proxy.edit = edit
    assert (kept_of(record, request.id).stands, edit(*held[0]), proxy.written[WEB_ADDRESS]) == (Stands.FAILED, LOST, before)
    lanes_on(proxy).begin_maintenance("site", "testing")
    assert (edit(*held[0]), proxy.written[WEB_ADDRESS]) == (LOST, BACK_SOON)


def test_a_request_that_asked_for_no_change_of_the_route_ends_with_the_gate_never_asked(record: Record) -> None:
    proxy = PretendProxy(PretendHost())
    worker = worker_on(proxy, record)
    for work in (RETIRE, END):
        request = record.write(work, CALLER, None)
        assert (worker.work_next(), kept_of(record, request.id).stands, proxy.gate.kept) == (True, Stands.DONE, {})


def test_a_retire_whose_fence_gets_no_answer_keeps_its_lane_from_the_next_request_and_ends_once_the_gate_answers(
    record: Record,
) -> None:
    proxy = lane_brought_up_with_a()
    worker = worker_on(proxy, record)
    proxy.while_awaiting = lambda: setattr(proxy.gate, "unreadable", "The proxy's container did not answer")
    retire = record.write(RETIRE, CALLER, None)
    assert worker.work_next() is True
    begin = record.write(BEGIN, CALLER, None)
    assert worker.work_next() is True
    left = kept_of(record, retire.id)
    assert (left.stands, [each.ended for each in left.attempts]) == (Stands.WORKING, ["unfinished", "unfinished"])
    assert left.error is not None and "It is not ended yet: The proxy's container did not answer" in left.error
    assert (kept_of(record, begin.id).stands, WEB_ADDRESS in proxy.written) == (Stands.WAITING, False)
    proxy.gate.unreadable = None
    assert worker.work_next() is True
    assert (kept_of(record, retire.id).stands, proxy.gate.kept[retire.generation].fenced >= 0) == (Stands.DONE, True)


def test_a_request_found_working_is_fenced_when_it_ends_though_this_attempt_asked_for_no_change(record: Record) -> None:
    proxy = PretendProxy(PretendHost())
    request = record.write(RETIRE, CALLER, None)
    assert record.take_up(request.id) is not None
    assert worker_on(proxy, record).work_next() is True
    assert (kept_of(record, request.id).stands, proxy.gate.kept[request.generation].fenced >= 0) == (Stands.DONE, True)


def test_giving_up_a_retire_that_needs_a_person_fences_every_attempt_of_it_before_its_lane_takes_another_request(
    record: Record,
) -> None:
    worker, lanes, proxy = serving_a(record)
    client = TestClient(create_app(lanes.read_status, record, worker.wake, NO_SECRETS, lanes))
    asked = client.post("/lanes/site/testing/retire", json={}).json()
    attempts = [record.take_up(asked["id"]) for _ in range(MOST_ATTEMPTS)]
    assert (worker.work_next(), kept_of(record, asked["id"]).stands) == (True, Stands.NEEDS_A_PERSON)
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


def test_a_retire_whose_removal_the_gate_never_makes_retires_no_container_and_does_not_end_as_done(record: Record) -> None:
    proxy = lane_brought_up_with_a()

    def never_made(web_address: str, generation: str, attempt: int, read: str, container: str | None) -> str:
        assert web_address and generation and attempt and read and container is None
        return LOST

    proxy.edit = never_made
    request = record.write(RETIRE, CALLER, None)
    assert worker_on(proxy, record).work_next() is True
    assert (kept_of(record, request.id).stands, WEB_ADDRESS in proxy.written, len(proxy.host.instances)) == (Stands.FAILED, True, 1)
