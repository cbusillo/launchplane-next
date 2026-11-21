from collections.abc import Callable
from datetime import UTC, datetime
from itertools import count
from typing import Any, cast

import pytest

from launchplane.act import Lanes
from launchplane.api import why_not_cancelled
from launchplane.instance import Verdict
from launchplane.page import line_of, request_note
from launchplane.record import Asked, Event, Interrupted, Kept, Record, Stands
from launchplane.telling import Said, Seen
from launchplane.worker import Worker
from tests.given_secrets import NO_SECRETS
from tests.pretend_host import NOT_LAUNCHPLANES_ROUTE, WEB_ADDRESS, WITH_DATA, LaunchplaneKilled, PretendProxy, numbered_names
from tests.pretend_record import PretendRecord, begun
from tests.test_act import lane_brought_up_with_a, lane_in_maintenance
from tests.test_host_answers import (
    DATABASE,
    END,
    MOVE_A,
    REFUSED,
    RETIRE,
    cancelled_while_checking,
    lane_whose_background_service_is_gone,
    lane_with_a_login_the_server_will_not_remove,
    worker_on,
)
from tests.test_route_edits import accepted_and_not_answered
from tests.test_worker import CALLER, MOVE_B, kept_of


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


# The line of the start says the host answered it changed, and the host shows no background service.
def a_start_answered_done_beside_a_host_that_shows_none(record: Record) -> tuple[PretendProxy, int]:
    proxy = lane_whose_background_service_is_gone()
    request = record.write(MOVE_A, CALLER, None)
    killed_after(len(["start", "await"]), proxy, record)
    proxy.host.background_services.clear()
    return proxy, request.id


def part_way_through_a_retire(record: Record) -> tuple[PretendProxy, int]:
    proxy = lane_brought_up_with_a()
    request = record.write(RETIRE.model_copy(update={"delete_data_set": "data-1"}), CALLER, None)
    killed_after(len(["unroute", "retire the background service", "retire the instance"]), proxy, record)
    return proxy, request.id


INTERRUPTED: dict[str, Callable[[Record], tuple[PretendProxy, int]]] = {
    "ending maintenance, its route change made and never answered": ending_maintenance_with_its_route_change_made_and_not_answered,
    "a line that says done beside a host that shows otherwise": a_start_answered_done_beside_a_host_that_shows_none,
    "part way through a retire": part_way_through_a_retire,
}


# What a fresh worker does to the host from there, and how the request then stands.
def carried_on(record: PretendRecord, proxy: PretendProxy, request: int) -> tuple[list[str], Kept, object]:
    calls: list[str] = []
    host = cast("Any", Recorded(proxy.host, "host", calls))
    lanes = Lanes(host, cast("Any", Recorded(proxy, "proxy", calls)), lambda _product: WITH_DATA, numbered_names())
    worker = Worker(record, lanes, NO_SECRETS)
    while kept_of(record, request).stands is Stands.WORKING:
        assert worker.work_next() is True
    left = (proxy.written, sorted(proxy.host.instances), sorted(proxy.host.background_services), sorted(proxy.host.data_sets))
    return calls, kept_of(record, request), left


@pytest.mark.parametrize("interrupted", INTERRUPTED.values(), ids=INTERRUPTED.keys())
def test_erasing_action_history_changes_the_account_but_not_the_next_host_action(
    monkeypatch: pytest.MonkeyPatch, interrupted: Callable[[Record], tuple[PretendProxy, int]]
) -> None:
    carried: list[tuple[list[str], Kept, object]] = []
    for erased in (False, True):
        numbers = count(1)
        monkeypatch.setattr(
            "tests.pretend_record.new_generation", lambda numbers=numbers: f"01a1ef36-0000-7000-8000-{next(numbers):012}"
        )
        record = PretendRecord()
        proxy, request = interrupted(record)
        kept = kept_of(record, request)
        assert [each for each in kept.events if each.action is not None], "the interruption left no action line to erase"
        if erased:
            record.change(request, events=[each for each in kept.events if each.action is None])
        carried.append(carried_on(record, proxy, request))
    (calls, with_history, left), (calls_erased, without_history, left_erased) = carried
    assert calls and (calls, left) == (calls_erased, left_erased)
    assert (with_history.stands, without_history.stands) == (Stands.DONE, Stands.DONE)
    assert with_history.answer != without_history.answer


def test_a_move_that_only_waited_and_checked_over_two_attempts_answers_unchanged(record: Record) -> None:
    proxy = lane_brought_up_with_a()
    proxy.loaded.clear()
    proxy.holds_delivery = True
    worker, request = worker_on(proxy, record), record.write(MOVE_A, CALLER, None)
    assert worker.work_next() is True
    proxy.holds_delivery = False
    assert worker.work_next() is True
    kept = kept_of(record, request.id)
    assert (
        kept.answer is not None
        and [each.did for each in kept.events] == [f"wait for the proxy to load the route of {WEB_ADDRESS}"] * 2
    )
    assert ([each.ended for each in kept.attempts], kept.answer["outcome"], kept.answer["did"]) == (
        ["unfinished", "done"],
        "unchanged",
        [],
    )


def test_a_clean_up_of_logins_that_removed_none_is_no_change_of_the_request(record: Record) -> None:
    proxy = lane_brought_up_with_a()
    proxy.host.change_database(DATABASE, unheld_logins=("i_left",))

    def held_by_the_time_it_is_read_again(database: str) -> list[str]:
        proxy.host.change_database(database, unheld_logins=())
        return []

    proxy.host.remove_unheld_logins = held_by_the_time_it_is_read_again
    request = record.write(MOVE_A, CALLER, None)
    assert worker_on(proxy, record).work_next() is True
    kept = kept_of(record, request.id)
    [line] = kept.events
    assert kept.answer is not None and line.outcome is not None
    assert (line.outcome.said, kept.answer["outcome"], kept.answer["did"]) == (Said.ALREADY, "unchanged", [])


def test_a_cancel_that_won_and_whose_clean_up_was_refused_is_a_cancel_that_won_on_the_page_and_to_a_cancel_asked_again(
    record: Record,
) -> None:
    kept = cancelled_while_checking(record, lane_with_a_login_the_server_will_not_remove())
    said = "The cancel won at the gate, and its lane was not put back whole"
    assert (kept.stands, kept.release_verdict, REFUSED in (kept.error or "")) == (Stands.FAILED, Verdict.ABANDONED, True)
    assert [(text or "").startswith(said) for text in (request_note(kept, {}), why_not_cancelled(kept))] == [True, True]


def test_a_request_that_stops_says_what_every_attempt_of_it_changed_before_that(record: Record) -> None:
    proxy = lane_whose_background_service_is_gone()
    request = record.write(MOVE_A, CALLER, None)
    killed_after(len(["start", "await"]), proxy, record)
    [instance] = proxy.host.instances
    proxy.unread[WEB_ADDRESS] = NOT_LAUNCHPLANES_ROUTE
    assert worker_on(proxy, record).work_next() is True
    kept = kept_of(record, request.id)
    assert kept.stands is Stands.FAILED and kept.error is not None
    assert f"Before that, this request did: start the background service of {instance}. Those changes stay." in kept.error


def test_a_line_is_written_before_its_change_is_sent_and_only_the_attempt_that_is_open_adds_to_it(record: Record) -> None:
    request = record.write(MOVE_B, CALLER, None)
    first = begun(record, request.id)
    line = record.about_to(first, "send testing.site.test to a", {"verb": "route", "subject": WEB_ADDRESS, "to": "a"})
    [written] = kept_of(record, request.id).events
    second = begun(record, request.id)
    with pytest.raises(Interrupted):
        record.complete(first, line, {"said": "changed"})
    record.complete(second, line, {"seen": Seen.OURS.value}, "the reply was lost")
    record.complete(second, line, {"seen_at": "2026-11-21T10:00:00+00:00"})
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
    "send x to a: sent, no reply": (LINE | {"action": ROUTE}, False),
    "send x to a: done": (LINE | {"action": ROUTE, "outcome": {"said": "changed"}}, False),
    "send x to a: found already as asked": (LINE | {"action": ROUTE, "outcome": {"said": "already as asked"}}, False),
    "send x to a, to put the lane back: refused": (LINE | {"action": ROUTE | {"back": True}, "outcome": {"said": "refused"}}, True),
    "send x to a: sent, no reply, then read as made by this request": (
        LINE | {"action": ROUTE, "outcome": {"seen": "made by this request"}, "error": "lost"},
        False,
    ),
    "send x to a": (LINE, True),
    "send x to a, refused": (LINE | {"error": "Connection refused"}, False),
}


@pytest.mark.parametrize(("shown", "line"), LINES.items(), ids=LINES.keys())
def test_the_page_says_how_far_each_change_is_known_to_have_got_and_shows_a_line_written_before_this_as_it_was(
    shown: str, line: tuple[dict[str, Any], bool]
) -> None:
    assert line_of(Event.model_validate(line[0]), line[1]) == shown


def test_a_request_asked_again_on_a_lane_already_as_asked_is_credited_with_nothing(record: Record) -> None:
    proxy = lane_brought_up_with_a()
    request = record.write(Asked.model_validate(MOVE_A.model_dump()), CALLER, None)
    assert worker_on(proxy, record).work_next() is True
    kept = kept_of(record, request.id)
    assert kept.answer is not None and (kept.answer["outcome"], kept.answer["did"], kept.answer["unanswered"]) == (
        "unchanged",
        [],
        [],
    )
