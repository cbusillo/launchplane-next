from collections.abc import Callable
from datetime import UTC, datetime
from itertools import count
from typing import Any, NoReturn

import pytest
from fastapi.testclient import TestClient

from launchplane.act import Lanes
from launchplane.api import create_app, why_not_cancelled
from launchplane.gate import Answer as GateAnswer
from launchplane.instance import BACK_SOON, ProxySeen, Verdict
from launchplane.page import cards, line_of, request_note
from launchplane.record import Event, Interrupted, Kept, Record, Reply, Sent, Stands
from launchplane.worker import Worker
from tests.given_secrets import NO_SECRETS
from tests.pretend_host import WEB_ADDRESS, WITH_DATA, LaunchplaneKilled, PretendProxy, numbered_names
from tests.pretend_record import PretendRecord, begun
from tests.test_act import lane_brought_up_with_a, lane_in_maintenance
from tests.test_host_answers import (
    BEGIN,
    END,
    REFUSED,
    RETIRE,
    cancelled_while_checking,
    lane_with_a_login_the_server_will_not_remove,
    worker_on,
)
from tests.test_route_edits import accepted_and_not_answered
from tests.test_worker import CALLER, MOVE_B, kept_of, serving_a


def route_lines(kept: Kept) -> list[Event]:
    return [each for each in kept.events if each.action is not None]


def credited(kept: Kept) -> list[str]:
    return [each.did for each in kept.events if each.is_credited]


def test_a_line_is_written_before_its_change_is_sent_and_only_the_attempt_that_is_open_adds_to_it(record: Record) -> None:
    request = record.write(MOVE_B, CALLER, None)
    first = begun(record, request.id)
    line = record.about_to(first, "send testing.site.test to a", Sent(web_address=WEB_ADDRESS, to="a"))
    [written] = kept_of(record, request.id).events
    second = begun(record, request.id)
    with pytest.raises(Interrupted):
        record.complete(first, line, Reply(made=True))
    record.complete(second, line, Reply(), "the reply was lost")
    read_at = datetime(2026, 11, 21, 10, tzinfo=UTC)
    record.complete(second, line, Reply(read_back_at=read_at))
    [read_back] = kept_of(record, request.id).events
    assert (written.action, written.outcome, written.is_unanswered, written.is_credited) == (
        Sent(web_address=WEB_ADDRESS, to="a"),
        None,
        True,
        False,
    )
    assert (read_back.outcome, read_back.error, read_back.is_credited, read_back.is_unanswered) == (
        Reply(read_back_at=read_at),
        "the reply was lost",
        True,
        False,
    )
    with pytest.raises(Interrupted):
        record.about_to(first, "send testing.site.test to b", Sent(web_address=WEB_ADDRESS, to="b"))


def test_an_edit_of_the_route_reaches_the_gate_only_once_its_line_is_in_the_record_and_the_gates_reply_goes_on_that_line(
    record: Record,
) -> None:
    proxy = lane_brought_up_with_a()
    edit, at_the_gate = proxy.edit, list[list[Event]]()

    def seen_at_the_gate(web_address: str, generation: str, attempt: int, read: str, container: str | None) -> str:
        at_the_gate.append(route_lines(kept_of(record, request.id)))
        return edit(web_address, generation, attempt, read, container)

    proxy.edit = seen_at_the_gate
    request = record.write(BEGIN, CALLER, None)
    assert worker_on(proxy, record).work_next() is True
    [[before]], [after] = at_the_gate, route_lines(kept_of(record, request.id))
    assert (before.did, before.outcome, before.id) == (after.did, None, after.id)
    assert before.action == Sent(web_address=WEB_ADDRESS, to=BACK_SOON)
    assert after.outcome is not None and (after.outcome.made, after.error, after.is_credited) == (True, None, True)


def test_a_switch_reaches_the_gate_only_once_its_line_is_in_the_record_and_the_gates_reply_goes_on_that_line(
    record: Record,
) -> None:
    worker, _, proxy = serving_a(record)
    switch, at_the_gate = proxy.switch, list[list[Event]]()

    def seen_at_the_gate(web_address: str, generation: str, attempt: int, candidate: str) -> GateAnswer:
        at_the_gate.append(route_lines(kept_of(record, request.id)))
        return switch(web_address, generation, attempt, candidate)

    proxy.switch = seen_at_the_gate
    request = record.write(MOVE_B, CALLER, None)
    assert worker.work_next() is True
    [[before]], [after] = at_the_gate, route_lines(kept_of(record, request.id))
    assert (before.did, before.outcome) == (f"switch {WEB_ADDRESS} to {proxy.written[WEB_ADDRESS]}", None)
    assert before.action == Sent(web_address=WEB_ADDRESS, to=proxy.written[WEB_ADDRESS])
    assert after.outcome is not None and (after.id, after.outcome.made, after.is_credited) == (before.id, True, True)


def test_ending_maintenance_whose_route_change_was_made_and_never_answered_says_ended_and_an_end_asked_after_it_claims_nothing(
    record: Record,
) -> None:
    proxy = lane_in_maintenance()
    worker = worker_on(proxy, record)
    accepted_and_not_answered(proxy, has_run=True)
    first = record.write(END, CALLER, None)
    assert (worker.work_next(), worker.work_next()) == (True, True)
    second = record.write(END, CALLER, None)
    assert worker.work_next() is True
    ended, after = kept_of(record, first.id), kept_of(record, second.id)
    [published] = route_lines(ended)
    assert published.outcome is not None and published.action is not None
    assert ((ended.answer or {})["outcome"], published.outcome.made, published.action.to, credited(ended)) == (
        "ended",
        None,
        proxy.written[WEB_ADDRESS],
        [published.did],
    )
    assert published.outcome.read_back_at is not None and "lost before the reply" in (published.error or "")
    assert ((after.answer or {})["outcome"], after.events) == ("not in maintenance", [])


def test_a_route_one_attempt_installed_is_not_put_down_to_the_line_of_an_earlier_attempt_that_sent_the_same_change(
    record: Record,
) -> None:
    proxy = lane_in_maintenance()
    worker = worker_on(proxy, record)
    accepted_and_not_answered(proxy, has_run=False)
    request = record.write(END, CALLER, None)
    assert worker.work_next() is True
    accepted_and_not_answered(proxy, has_run=True)
    assert (worker.work_next(), worker.work_next()) == (True, True)
    kept = kept_of(record, request.id)
    never_ran, installed = route_lines(kept)
    assert (never_ran.did, never_ran.attempt, installed.attempt) == (installed.did, 1, 2)
    assert (kept.stands, never_ran.is_unanswered, installed.is_credited, credited(kept)) == (
        Stands.DONE,
        True,
        True,
        [installed.did],
    )


def test_a_request_that_ended_between_the_two_readings_of_a_status_shows_once_on_its_card_as_ended(record: Record) -> None:
    worker, lanes, _ = serving_a(record)
    request = record.write(MOVE_B, CALLER, None)
    read_before_it_ended = record.unfinished()
    assert worker.work_next() is True
    lanes.read_unfinished, lanes.read_ended = lambda: read_before_it_ended, record.ended_lately
    status = lanes.read_status()
    [row] = [row for card in cards(status) for row in card.rows if row.name == f"Request {request.id}"]
    assert ([pill.text for pill in row.pills], status.requests) == (["done"], [])


def test_a_retire_killed_with_its_removal_of_the_route_sent_and_then_cancelled_keeps_the_unanswered_line_on_its_card(
    record: Record,
) -> None:
    proxy = lane_brought_up_with_a()

    def killed_once_sent(web_address: str, generation: str, attempt: int, read: str, container: str | None) -> NoReturn:
        raise LaunchplaneKilled(web_address, generation, attempt, read, container)

    edit, proxy.edit = proxy.edit, killed_once_sent
    request = record.write(RETIRE, CALLER, None)
    with pytest.raises(LaunchplaneKilled):
        worker_on(proxy, record).work_next()
    proxy.edit = edit
    record.cancel(request.id, CALLER)
    assert worker_on(proxy, record).work_next() is True
    kept = kept_of(record, request.id)
    assert (kept.stands, WEB_ADDRESS in proxy.written) == (Stands.CANCELLED, True)
    assert (request_note(kept, {}) or "").endswith(f"Its lines: remove the route of {WEB_ADDRESS}: may have been sent, no answer.")


def test_a_route_as_asked_that_another_hand_installed_is_not_put_down_to_the_request_whose_change_had_no_answer(
    record: Record,
) -> None:
    proxy = lane_in_maintenance()
    [instance] = proxy.host.instances
    accepted_and_not_answered(proxy, has_run=False)
    request = record.write(END, CALLER, None)
    worker = worker_on(proxy, record)
    assert worker.work_next() is True
    proxy.route(WEB_ADDRESS, instance)
    assert worker.work_next() is True
    kept = kept_of(record, request.id)
    [line] = route_lines(kept)
    assert (kept.stands, (kept.answer or {})["outcome"], line.is_unanswered, credited(kept)) == (
        Stands.DONE,
        "not in maintenance",
        True,
        [],
    )
    assert line_of(line, attempt_is_open=False) == f"send {WEB_ADDRESS} to {instance}: may have been sent, no answer"


def test_a_switch_that_lands_after_the_next_attempts_first_reading_is_put_down_to_its_request_and_is_in_the_moves_account(
    record: Record,
) -> None:
    worker, _, proxy = serving_a(record)
    switch, read = proxy.switch, proxy.read
    sent: list[tuple[str, str, int, str]] = []

    def killed_once_sent(web_address: str, generation: str, attempt: int, candidate: str) -> NoReturn:
        sent.append((web_address, generation, attempt, candidate))
        raise LaunchplaneKilled

    def read_and_then_it_lands() -> ProxySeen:
        before_it_landed = read()
        proxy.read = read
        switch(*sent[0])
        return before_it_landed

    proxy.switch = killed_once_sent
    request = record.write(MOVE_B, CALLER, None)
    with pytest.raises(LaunchplaneKilled):
        worker.work_next()
    proxy.switch, proxy.read = switch, read_and_then_it_lands
    assert worker.work_next() is True
    kept = kept_of(record, request.id)
    [line] = route_lines(kept)
    assert kept.answer is not None and line.outcome is not None
    assert (kept.stands, kept.answer["outcome"], line.outcome.made, line.is_credited) == (Stands.DONE, "switched", None, True)
    assert kept.answer["did"] == [f"start the instance {sent[0][3]} of site:b", line.did, "retire a"]


# Every call made on a part of the pretend host, in order and with what it was given.
class Recorded:
    def __init__(self, part: object, name: str, calls: list[str]) -> None:
        self.part, self.name, self.calls = part, name, calls

    def __getattr__(self, attribute: str) -> Any:  # noqa: ANN401 whatever the part's attribute is
        value = getattr(self.part, attribute)
        if attribute == "gate":
            return Recorded(value, "gate", self.calls)
        if not callable(value):
            return value

        def called(*given: object, **named: object) -> object:
            self.calls.append(f"{self.name}.{attribute}{given}{named or ''}")
            return value(*given, **named)

        return called


def killed_after(actions: int, proxy: PretendProxy, record: Record) -> None:
    proxy.host.killed_after = len(proxy.host.actions) + actions
    with pytest.raises(LaunchplaneKilled):
        worker_on(proxy, record).work_next()
    proxy.host.killed_after = None


def ending_maintenance_with_its_route_change_made_and_not_answered(record: Record) -> tuple[PretendProxy, int]:
    proxy = lane_in_maintenance()
    accepted_and_not_answered(proxy, has_run=True)
    request = record.write(END, CALLER, None)
    assert worker_on(proxy, record).work_next() is True
    return proxy, request.id


# The line of the change to the back soon page says the gate made it, and the routes file sends visitors to the instance.
def a_route_change_answered_done_beside_a_host_that_shows_otherwise(record: Record) -> tuple[PretendProxy, int]:
    proxy = lane_brought_up_with_a()
    instance = proxy.written[WEB_ADDRESS]
    request = record.write(BEGIN, CALLER, None)
    killed_after(len(["show back soon", "stop the background service"]), proxy, record)
    proxy.route(WEB_ADDRESS, instance)
    return proxy, request.id


def part_way_through_a_retire(record: Record) -> tuple[PretendProxy, int]:
    proxy = lane_brought_up_with_a()
    request = record.write(RETIRE.model_copy(update={"delete_data_set": "data-1"}), CALLER, None)
    killed_after(len(["unroute", "retire the background service", "retire the instance"]), proxy, record)
    return proxy, request.id


INTERRUPTED: dict[str, Callable[[Record], tuple[PretendProxy, int]]] = {
    "ending maintenance, its route change made and never answered": ending_maintenance_with_its_route_change_made_and_not_answered,
    "a line that says done beside a host that shows otherwise": a_route_change_answered_done_beside_a_host_that_shows_otherwise,
    "part way through a retire": part_way_through_a_retire,
}


# What a fresh worker does to the host from there, and how the request then stands.
def carried_on(record: PretendRecord, proxy: PretendProxy, request: int) -> tuple[list[str], Kept, object]:
    calls: list[str] = []
    host: Any = Recorded(proxy.host, "host", calls)
    routes: Any = Recorded(proxy, "proxy", calls)
    worker = Worker(record, Lanes(host, routes, lambda _product: WITH_DATA, numbered_names()), NO_SECRETS)
    while kept_of(record, request).stands is Stands.WORKING:
        assert worker.work_next() is True
    left = (proxy.written, sorted(proxy.host.instances), sorted(proxy.host.background_services), sorted(proxy.host.data_sets))
    return calls, kept_of(record, request), left


@pytest.mark.parametrize("interrupted", INTERRUPTED.values(), ids=INTERRUPTED.keys())
def test_with_every_line_of_a_route_change_deleted_a_fresh_worker_makes_the_same_calls_on_the_host_in_the_same_order(
    monkeypatch: pytest.MonkeyPatch, interrupted: Callable[[Record], tuple[PretendProxy, int]]
) -> None:
    def carried_from_the_interruption(*, erased: bool) -> tuple[list[str], Kept, object]:
        generations = count(1)
        monkeypatch.setattr("tests.pretend_record.new_generation", lambda: f"01a1ef36-0000-7000-8000-{next(generations):012}")
        record = PretendRecord()
        proxy, request = interrupted(record)
        kept = kept_of(record, request)
        assert route_lines(kept), "the interruption left no line of a route change to delete"
        if erased:
            record.change(request, events=[each for each in kept.events if each.action is None])
        return carried_on(record, proxy, request)

    calls, with_lines, left = carried_from_the_interruption(erased=False)
    calls_erased, without_lines, left_erased = carried_from_the_interruption(erased=True)
    assert calls and (calls, left) == (calls_erased, left_erased)
    assert (with_lines.stands, without_lines.stands) == (Stands.DONE, Stands.DONE)
    assert credited(with_lines) != credited(without_lines)


LINE: dict[str, Any] = {"id": 1, "at": datetime(2026, 11, 21, tzinfo=UTC), "attempt": 1, "did": "send x to a", "error": None}
ROUTE = {"web_address": "x", "to": "a"}
READ_BACK = {"read_back_at": "2026-11-21T10:00:00Z"}
LINES = {
    "about to send x to a": (LINE | {"action": ROUTE}, True),
    "send x to a: may have been sent, no answer": (LINE | {"action": ROUTE}, False),
    "send x to a: may have been sent, no answer ": (LINE | {"action": ROUTE, "outcome": {}, "error": "lost"}, True),
    "send x to a: done": (LINE | {"action": ROUTE, "outcome": {"made": True}}, False),
    "send x to a: found already as asked": (LINE | {"action": ROUTE, "outcome": {"made": False, "already": True}}, False),
    "send x to a: refused": (LINE | {"action": ROUTE, "outcome": {"made": False}, "error": "not the one read"}, True),
    "send x to a: may have been sent, no answer, then read as made by this request": (
        LINE | {"action": ROUTE, "outcome": READ_BACK, "error": "lost"},
        False,
    ),
    "send x to a": (LINE, True),
    "send x to a, refused": (LINE | {"error": "Connection refused"}, False),
}


@pytest.mark.parametrize(("shown", "line"), LINES.items(), ids=LINES.keys())
def test_the_page_says_how_far_a_change_of_the_route_is_known_to_have_got_and_shows_any_other_line_as_it_was_told(
    shown: str, line: tuple[dict[str, Any], bool]
) -> None:
    assert line_of(Event.model_validate(line[0]), line[1]) == shown.strip()


def test_a_cancel_that_won_and_could_not_be_put_back_is_said_so_from_the_verdict_kept_and_not_from_how_its_error_begins(
    record: Record,
) -> None:
    kept = cancelled_while_checking(record, lane_with_a_login_the_server_will_not_remove())
    assert (kept.stands, kept.release_verdict, REFUSED in (kept.error or "")) == (Stands.FAILED, Verdict.ABANDONED, True)
    assert not (kept.error or "").startswith("The cancel won")
    assert (request_note(kept, {}), why_not_cancelled(kept)) == (kept.error, f"{kept.error} Request {kept.id} ended as failed.")
    never_asked = kept.model_copy(update={"cancel_asked_at": None})
    assert (
        why_not_cancelled(never_asked)
        == f"Request {kept.id} ended as failed, so it was not cancelled. Nothing was changed by this call."
    )


def test_a_move_ends_with_the_gates_verdict_kept_and_a_request_that_is_no_move_keeps_none(record: Record) -> None:
    worker, lanes, _ = serving_a(record)
    move = record.write(MOVE_B, CALLER, None)
    assert worker.work_next() is True
    same_again = record.write(MOVE_B, CALLER, None)
    begin = record.write(BEGIN.model_copy(update={"lane": "other"}), CALLER, None)
    while worker.work_next():
        pass
    answered = TestClient(create_app(lanes.read_status, record, worker.wake, NO_SECRETS, lanes)).get(f"/requests/{move.id}")
    assert [kept_of(record, each.id).release_verdict for each in (move, same_again, begin)] == [
        Verdict.COMMITTED,
        Verdict.NONE,
        None,
    ]
    assert answered.json()["release_verdict"] == "committed"


def test_a_route_another_hand_removed_while_a_retire_was_in_flight_is_recorded_as_already_and_not_put_down_to_the_retire(
    record: Record,
) -> None:
    proxy = lane_brought_up_with_a()
    edit, was = proxy.edit, proxy.written[WEB_ADDRESS]

    def removed_just_before(web_address: str, generation: str, attempt: int, read: str, container: str | None) -> str:
        proxy.edit = edit
        del proxy.written[web_address]
        return edit(web_address, generation, attempt, read, container)

    proxy.edit = removed_just_before
    request = record.write(RETIRE, CALLER, None)
    assert worker_on(proxy, record).work_next() is True
    kept = kept_of(record, request.id)
    [line] = route_lines(kept)
    assert line.outcome is not None and kept.answer is not None
    assert (line.outcome.already, line.outcome.made, line.error, credited(kept)) == (True, False, None, [])
    assert line_of(line, attempt_is_open=False) == f"remove the route of {WEB_ADDRESS}: found already as asked"
    assert (kept.answer["outcome"], kept.answer["route_removed_to_container"]) == ("retired", was)


def test_a_cancel_asked_before_a_lost_reply_was_recovered_leaves_the_completed_line_on_the_card_beside_the_cancels_note(
    record: Record,
) -> None:
    proxy = lane_in_maintenance()
    worker = worker_on(proxy, record)
    accepted_and_not_answered(proxy, has_run=True)
    request = record.write(END, CALLER, None)
    assert worker.work_next() is True
    record.cancel(request.id, CALLER)
    assert worker.work_next() is True
    kept = kept_of(record, request.id)
    note = request_note(kept, {}) or ""
    assert (kept.stands, (kept.answer or {})["outcome"], kept.cancel_asked_at is not None) == (Stands.DONE, "ended", True)
    assert f"A cancel by {CALLER} was asked and not carried out. It ended as done. Its lines: " in note
    assert note.endswith(
        f"send {WEB_ADDRESS} to {proxy.written[WEB_ADDRESS]}: may have been sent, no answer, then read as made by this request."
    )


def test_a_request_that_has_just_ended_stays_on_its_lanes_card_with_its_last_lines_and_is_not_among_the_cancels(
    record: Record,
) -> None:
    worker, lanes, proxy = serving_a(record)
    lanes.read_ended = record.ended_lately
    first = record.write(MOVE_B, CALLER, None)
    assert worker.work_next() is True
    second = record.write(MOVE_B.model_copy(update={"build": "site:c"}), CALLER, None)
    assert worker.work_next() is True
    [card] = cards(lanes.read_status())
    [row] = [row for row in card.rows if row.name.startswith("Request")]
    assert (first.id < second.id, row.name, [pill.text for pill in row.pills]) == (True, f"Request {second.id}", ["done"])
    assert row.note is not None and row.note.startswith("It ended as done. Its lines: ")
    assert f"switch {WEB_ADDRESS} to {proxy.written[WEB_ADDRESS]}: done" in row.note
    client = TestClient(create_app(lanes.read_status, record, worker.wake, NO_SECRETS, lanes))
    assert (client.get("/running").json()["cancels"], client.get("/running").json()["requests"]) == ([], [])
    waiting = record.write(MOVE_B, CALLER, None)
    record.cancel(waiting.id, CALLER)
    [cancelled] = client.get("/running").json()["cancels"]
    assert (cancelled["id"], cancelled["stands"]) == (waiting.id, "cancelled")
    assert not {"events", "attempts", "release_verdict"} & set(cancelled)
