from collections.abc import Callable
from datetime import UTC, datetime
from itertools import count
from typing import Any, NoReturn

import pytest

from launchplane.act import Lanes
from launchplane.api import why_not_cancelled
from launchplane.docker_answers import ContainerHostError
from launchplane.instance import BACK_SOON, DataSet, ProxySeen, Verdict
from launchplane.page import cards, line_of, request_note
from launchplane.record import Event, Interrupted, Kept, Record, Reply, Sent, Stands, how_cancelled
from launchplane.steps import NOT_KNOWN
from launchplane.telling import Said, Seen
from launchplane.worker import Worker
from tests.given_secrets import NO_SECRETS
from tests.pretend_host import NOT_LAUNCHPLANES_ROUTE, WEB_ADDRESS, WITH_DATA, LaunchplaneKilled, PretendProxy, numbered_names
from tests.pretend_record import PretendRecord, begun
from tests.test_act import lane_brought_up_with_a, lane_in_maintenance
from tests.test_host_answers import (
    BEGIN,
    END,
    MOVE_A,
    REFUSED,
    RETIRE,
    cancelled_while_checking,
    lane_with_a_login_the_server_will_not_remove,
    worker_on,
)
from tests.test_route_edits import accepted_and_not_answered
from tests.test_worker import CALLER, MOVE_B, kept_of, serving_a

BY_HAND = "a call from the address 192.0.2.7"


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
    lanes = Lanes(host, routes, lambda _product: WITH_DATA, numbered_names())
    worker = Worker(record, lanes, NO_SECRETS)
    while kept_of(record, request).stands is Stands.WORKING:
        assert worker.work_next() is True
    left = (proxy.written, sorted(proxy.host.instances), sorted(proxy.host.background_services), sorted(proxy.host.data_sets))
    return calls, kept_of(record, request), left


def credited(kept: Kept) -> list[str]:
    return [each.did for each in kept.events if each.is_credited]


@pytest.mark.parametrize("interrupted", INTERRUPTED.values(), ids=INTERRUPTED.keys())
def test_erasing_action_history_changes_the_account_but_not_the_next_host_action(
    monkeypatch: pytest.MonkeyPatch, interrupted: Callable[[Record], tuple[PretendProxy, int]]
) -> None:
    def carried_from_the_interruption(*, erased: bool) -> tuple[list[str], Kept, object]:
        generations = count(1)
        monkeypatch.setattr("tests.pretend_record.new_generation", lambda: f"01a1ef36-0000-7000-8000-{next(generations):012}")
        record = PretendRecord()
        proxy, request = interrupted(record)
        kept = kept_of(record, request)
        assert [each for each in kept.events if each.action is not None], "the interruption left no action line to erase"
        if erased:
            record.change(request, events=[each for each in kept.events if each.action is None])
        return carried_on(record, proxy, request)

    calls, with_history, left = carried_from_the_interruption(erased=False)
    calls_erased, without_history, left_erased = carried_from_the_interruption(erased=True)
    assert calls and (calls, left) == (calls_erased, left_erased)
    assert (with_history.stands, without_history.stands) == (Stands.DONE, Stands.DONE)
    assert credited(with_history) != credited(without_history)


def test_a_request_asked_again_on_a_lane_already_as_asked_answers_unchanged_and_says_nothing_is_not_known(record: Record) -> None:
    proxy = lane_brought_up_with_a()
    request = record.write(MOVE_A, CALLER, None)
    assert worker_on(proxy, record).work_next() is True
    kept = kept_of(record, request.id)
    assert kept.answer is not None
    assert (kept.answer["outcome"], kept.answer["did"], kept.answer["not_known"], kept.events) == ("unchanged", [], None, [])


def test_a_move_taken_up_again_that_then_finds_nothing_to_do_says_not_known_and_never_unchanged(record: Record) -> None:
    proxy = lane_brought_up_with_a()
    proxy.loaded.clear()
    proxy.holds_delivery = True
    worker, request = worker_on(proxy, record), record.write(MOVE_A, CALLER, None)
    assert worker.work_next() is True
    proxy.holds_delivery = False
    assert worker.work_next() is True
    kept = kept_of(record, request.id)
    assert kept.answer is not None
    assert ([each.ended for each in kept.attempts], kept.answer["outcome"], kept.answer["not_known"]) == (
        ["unfinished", "done"],
        "not known",
        NOT_KNOWN,
    )
    [row] = [row for row in cards(serving_the_page(proxy, record).read_status())[0].rows if row.name.startswith("Request")]
    assert NOT_KNOWN in (row.note or "")


def serving_the_page(proxy: PretendProxy, record: Record) -> Lanes:
    return Lanes(proxy.host, proxy, lambda _product: WITH_DATA, read_unfinished=record.unfinished, read_ended=record.ended_lately)


def test_a_request_that_stops_says_nothing_was_changed_only_on_its_first_attempt_and_not_known_once_taken_up_again(
    record: Record,
) -> None:
    first, again = lane_brought_up_with_a(), lane_brought_up_with_a()
    first.unread[WEB_ADDRESS] = NOT_LAUNCHPLANES_ROUTE
    stopped_at_once = record.write(MOVE_A, CALLER, None)
    assert worker_on(first, record).work_next() is True
    again.host.background_services.clear()
    interrupted = record.write(MOVE_A.model_copy(update={"lane": "other"}), CALLER, None)
    assert begun(record, interrupted.id) > 0
    again.unread["other.site.test"] = NOT_LAUNCHPLANES_ROUTE
    assert worker_on(again, record).work_next() is True
    errors = [kept_of(record, each.id).error or "" for each in (stopped_at_once, interrupted)]
    assert [("Nothing was changed." in error, NOT_KNOWN in error) for error in errors] == [(True, False), (False, True)]


def test_a_cancel_that_won_and_whose_clean_up_was_refused_is_a_cancel_that_won_on_the_page_and_to_a_cancel_asked_again(
    record: Record,
) -> None:
    kept = cancelled_while_checking(record, lane_with_a_login_the_server_will_not_remove())
    said = "The cancel won at the gate, and its lane was not put back whole"
    assert (kept.stands, kept.release_verdict, REFUSED in (kept.error or "")) == (Stands.FAILED, Verdict.ABANDONED, True)
    assert [(text or "").startswith(said) for text in (request_note(kept, {}), why_not_cancelled(kept))] == [True, True]
    never_asked = kept.model_copy(update={"cancel_asked_at": None, "release_verdict": None})
    assert (
        why_not_cancelled(never_asked)
        == f"Request {kept.id} ended as failed, so it was not cancelled. Nothing was changed by this call."
    )


def test_a_line_is_written_before_its_change_is_sent_and_only_the_attempt_that_is_open_adds_to_it(record: Record) -> None:
    request = record.write(MOVE_B, CALLER, None)
    first = begun(record, request.id)
    line = record.about_to(first, "send testing.site.test to a", Sent(verb="route", subject=WEB_ADDRESS, to="a"))
    [written] = kept_of(record, request.id).events
    second = begun(record, request.id)
    with pytest.raises(Interrupted):
        record.complete(first, line, Reply(said=Said.CHANGED))
    record.complete(second, line, Reply(seen=Seen.OURS), "the reply was lost")
    record.complete(second, line, Reply(seen_at=datetime(2026, 11, 21, 10, tzinfo=UTC)))
    [read_back] = kept_of(record, request.id).events
    assert (written.action is not None, written.outcome, written.is_unanswered, written.is_credited) == (True, None, True, False)
    assert read_back.outcome is not None and (read_back.outcome.said, read_back.outcome.seen, read_back.error) == (
        None,
        Seen.OURS,
        "the reply was lost",
    )
    assert (read_back.is_credited, read_back.outcome.seen_at) == (True, datetime(2026, 11, 21, 10, tzinfo=UTC))


LINE: dict[str, Any] = {"id": 1, "at": datetime(2026, 11, 21, tzinfo=UTC), "attempt": 1, "did": "send x to a", "error": None}
ROUTE = {"verb": "route", "subject": "x", "to": "a"}
LINES = {
    "about to send x to a": (LINE | {"action": ROUTE}, True),
    "send x to a: may have been sent, no answer": (LINE | {"action": ROUTE}, False),
    "send x to a: done": (LINE | {"action": ROUTE, "outcome": {"said": "changed"}}, False),
    "send x to a: found already as asked": (LINE | {"action": ROUTE, "outcome": {"said": "already as asked"}}, False),
    "send x to a: refused": (LINE | {"action": ROUTE, "outcome": {"said": "refused"}}, True),
    "send x to a: may have been sent, no answer, then read as made by this request": (
        LINE | {"action": ROUTE, "outcome": {"seen": "made by this request"}, "error": "lost"},
        False,
    ),
    "send x to a": (LINE, True),
    "send x to a, refused": (LINE | {"error": "Connection refused"}, False),
}


@pytest.mark.parametrize(("shown", "line"), LINES.items(), ids=LINES.keys())
def test_the_page_says_how_far_a_change_of_the_route_is_known_to_have_got_and_shows_any_other_line_as_a_note(
    shown: str, line: tuple[dict[str, Any], bool]
) -> None:
    assert line_of(Event.model_validate(line[0]), line[1]) == shown


# The request as it ended, its answer, its one line for a change of the route, and what that line was read back as.
def ended_with_its_route_line(record: Record, request: int) -> tuple[Kept, dict[str, Any], Event, Seen | None]:
    kept = kept_of(record, request)
    [line] = [each for each in kept.events if each.is_route]
    assert kept.answer is not None and line.outcome is not None
    return kept, kept.answer, line, line.outcome.seen


def test_a_switch_that_lands_after_the_next_attempts_first_reading_is_completed_as_the_requests_own_by_a_later_reading(
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
    kept, answer, line, seen = ended_with_its_route_line(record, request.id)
    assert (kept.stands, answer["outcome"], seen, line.is_credited) == (Stands.DONE, "switched", Seen.OURS, True)


def test_a_route_as_asked_that_another_hand_installed_is_not_the_requests_own(record: Record) -> None:
    proxy = lane_in_maintenance()
    [instance] = proxy.host.instances
    accepted_and_not_answered(proxy, has_run=False)
    request = record.write(END, CALLER, None)
    worker = worker_on(proxy, record)
    assert worker.work_next() is True
    proxy.route(WEB_ADDRESS, instance)
    assert worker.work_next() is True
    kept, answer, line, seen = ended_with_its_route_line(record, request.id)
    assert (kept.stands, answer["outcome"], seen, line.is_unanswered) == (Stands.DONE, "not in maintenance", Seen.AS_ASKED, True)


def test_a_cancel_whose_clean_up_had_no_answer_says_what_it_did_is_not_known_and_not_that_nothing_had_to_be_put_back(
    record: Record,
) -> None:
    worker, _, proxy = serving_a(record)
    request, retire = record.write(MOVE_B, CALLER, None), proxy.host.retire_instance

    def killed_once_removed(container: str) -> NoReturn:
        retire(container)
        raise LaunchplaneKilled

    def cancel(_container: str) -> None:
        record.cancel(request.id, BY_HAND)
        proxy.host.retire_instance = killed_once_removed

    proxy.while_checking = cancel
    with pytest.raises(LaunchplaneKilled):
        worker.work_next()
    proxy.host.retire_instance = retire
    assert worker.work_next() is True
    kept = kept_of(record, request.id)
    said = how_cancelled(kept)
    assert (kept.stands, "Nothing on the host had to be put back" in said, NOT_KNOWN in said) == (Stands.CANCELLED, False, True)


def test_a_retire_cancelled_with_its_named_data_set_part_deleted_does_not_say_the_lane_can_be_moved_back_onto_it(
    record: Record,
) -> None:
    proxy = lane_brought_up_with_a()

    def connection_lost_once_the_volume_is_gone(found: DataSet) -> NoReturn:
        proxy.host.change_data_set("site", "testing", found.data_set, volumes=(), databases=())
        raise ContainerHostError(f"Deleting the data set {found.data_set} on test failed: the connection was lost")

    proxy.host.delete_data_set = connection_lost_once_the_volume_is_gone
    worker, request = worker_on(proxy, record), record.write(RETIRE.model_copy(update={"delete_data_set": "data-1"}), CALLER, None)
    assert worker.work_next() is True
    record.cancel(request.id, BY_HAND)
    assert worker.work_next() is True
    kept = kept_of(record, request.id)
    assert kept.stands is Stands.DONE and kept.answer is not None and kept.cancel_refused is not None
    assert (kept.answer["not_known"], kept.answer["kept_data_sets"]) == (NOT_KNOWN, ["data-1"])
    assert ("brings it back on its data set" in kept.cancel_refused, "is kept" in kept.cancel_refused) == (False, False)


def test_a_request_that_has_just_ended_stays_on_its_lanes_card_with_its_last_lines(record: Record) -> None:
    worker, lanes, proxy = serving_a(record)
    lanes.read_ended = record.ended_lately
    first = record.write(MOVE_B, CALLER, None)
    assert worker.work_next() is True
    second = record.write(MOVE_B.model_copy(update={"build": "site:c"}), CALLER, None)
    assert worker.work_next() is True
    [card] = cards(lanes.read_status())
    [row] = [row for row in card.rows if row.name.startswith("Request")]
    new = proxy.written[WEB_ADDRESS]
    assert (first.id < second.id, row.name, [pill.text for pill in row.pills]) == (True, f"Request {second.id}", ["done"])
    assert row.note is not None and row.note.startswith("It ended as done. Its lines: ")
    assert f"switch {WEB_ADDRESS} to {new}: done" in row.note


def test_a_route_already_as_asked_when_the_gate_is_sent_the_change_is_no_change_of_the_request(record: Record) -> None:
    proxy = lane_brought_up_with_a()
    edit = proxy.edit

    def already_there(web_address: str, generation: str, attempt: int, read: str, container: str | None) -> str:
        proxy.edit = edit
        edit(web_address, generation, attempt, read, container)
        return edit(web_address, generation, attempt, read, container)

    proxy.edit = already_there
    request = record.write(BEGIN, CALLER, None)
    assert worker_on(proxy, record).work_next() is True
    kept = kept_of(record, request.id)
    line = next(each for each in kept.events if each.is_route)
    assert line.outcome is not None and line.action is not None
    assert (line.action.to, line.outcome.said, line.is_credited) == (BACK_SOON, Said.ALREADY, False)
