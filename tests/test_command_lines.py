import json
from collections.abc import Callable
from dataclasses import dataclass, field
from itertools import count
from typing import Any

import pytest

from launchplane.container_host import ContainerHost
from launchplane.data_set import RULES_AS_READ
from launchplane.docker_answers import ContainerHostRefused
from launchplane.page import line_of, lines_of
from launchplane.record import Event, Interrupted, Kept, Record, Reply, Sent, Stands
from launchplane.steps import Command
from launchplane.telling import Answered, Said, Witness
from tests.pretend_host import GENERATION, LaunchplaneKilled, PretendProxy
from tests.pretend_record import PretendRecord
from tests.recording_docker import ExecResult, RecordingContainer, RecordingDocker
from tests.test_act import lane_brought_up_with_a
from tests.test_container_host import DATABASE, LINK, TARGET, WEB, database_made, given_to
from tests.test_host_answers import MOVE_A, worker_on
from tests.test_route_lines import INTERRUPTED, carried_on
from tests.test_worker import BUILD_A, CALLER, MOVE_B, kept_of, serving_a


# What a call is given in place of the record: each line as it was written and answered, beside what Docker was sent.
@dataclass
class Told:
    docker: RecordingDocker = field(default_factory=RecordingDocker)
    lines: list[tuple[str, Said | str | None]] = field(default_factory=list[tuple[str, Said | str | None]])
    credited: list[Command] = field(default_factory=list[Command])
    # What Docker had been sent for each container when each line was written.
    sent_before: dict[str, list[str]] = field(default_factory=dict[str, list[str]])

    def witness(self) -> Witness:
        def about_to_send(did: str, command: Command) -> Answered:
            place = len(self.lines)
            self.lines.append((did, "not answered"))
            made = self.docker.containers.by_name.get(command.container)
            self.sent_before[did] = ["not made"] if made is None else list(made.calls)

            def answered(said: Said | None, _error: str | None) -> None:
                self.lines[place] = (did, said)

            return answered

        return Witness(about_to_send=about_to_send, credit=self.credited.append)


def a_part_running(told: Told) -> tuple[ContainerHost, RecordingContainer]:
    host = ContainerHost(told.docker.as_client(), "test")
    host.start_instance(TARGET, WEB, None)
    return host, told.docker.containers.by_name[WEB]


def test_a_retire_whose_stop_docker_answers_and_whose_removal_it_refuses_leaves_the_stop_done_and_the_removal_refused() -> None:
    told = Told()
    host, part = a_part_running(told)
    part.refuses = {"remove"}
    with pytest.raises(ContainerHostRefused):
        host.retire_instance(WEB, told.witness())
    assert told.lines == [(f"stop {WEB}", Said.CHANGED), (f"remove {WEB}", Said.REFUSED)]
    assert (told.credited, part.status, part.removed) == ([Command(verb="stop", container=WEB)], "exited", False)


def test_a_stop_of_a_part_read_as_already_stopped_is_sent_all_the_same_and_is_recorded_as_found_so_with_no_credit() -> None:
    told = Told()
    host, part = a_part_running(told)
    host.stop_part(WEB)
    host.stop_part(WEB, told.witness())
    host.retire_instance(WEB, told.witness())
    assert told.lines == [(f"stop {WEB}", Said.ALREADY), (f"stop {WEB}", Said.ALREADY), (f"remove {WEB}", Said.CHANGED)]
    assert told.credited == [Command(verb="remove", container=WEB)]
    assert part.calls == ["start", "stop within 30 s", "stop within 30 s", "stop within 30 s", "remove"]


def test_a_removal_of_a_part_already_gone_sends_docker_nothing_and_is_recorded_as_found_so_with_no_credit() -> None:
    told = Told()
    ContainerHost(told.docker.as_client(), "test").retire_instance(WEB, told.witness())
    assert (told.lines, told.credited) == ([(f"remove {WEB}", Said.ALREADY)], [])


def test_a_start_of_a_part_read_as_already_running_earns_no_credit_and_one_of_a_stopped_part_does() -> None:
    told = Told()
    host, _ = a_part_running(told)
    host.start_part(WEB, told.witness())
    host.stop_part(WEB)
    host.start_part(WEB, told.witness())
    assert told.lines == [(f"start {WEB}", Said.ALREADY), (f"start {WEB}", Said.CHANGED)]
    assert told.credited == [Command(verb="start", container=WEB)]


def test_a_make_docker_answers_and_a_start_it_then_refuses_leave_the_make_done() -> None:
    told = Told()
    create = told.docker.containers.create

    def create_one_that_will_not_start(_image: str, _command: list[str] | None = None, **options: object) -> RecordingContainer:
        made = create(_image, _command, **options)
        made.refuses = {"start"}
        return made

    told.docker.containers.create = create_one_that_will_not_start
    with pytest.raises(ContainerHostRefused):
        ContainerHost(told.docker.as_client(), "test").start_instance(TARGET, WEB, None, GENERATION, told.witness())
    assert told.lines == [(f"make {WEB}", Said.CHANGED), (f"start {WEB}", Said.REFUSED)]
    assert told.credited == [Command(verb="make", container=WEB)]


def test_each_command_for_a_part_reaches_docker_only_once_its_line_is_written_and_the_line_before_it_is_answered() -> None:
    told = Told()
    host = database_made(told.docker)
    told.docker.containers.by_name[DATABASE].answers = [
        ExecResult(0, json.dumps(RULES_AS_READ).encode()),
        ExecResult(0, b"[]"),
        ExecResult(0),
    ]
    answered_when_written: list[list[tuple[str, Said | str | None]]] = []
    witness = told.witness()
    write = witness.about_to_send

    def written_after_the_last_was_answered(did: str, command: Command) -> Answered:
        answered_when_written.append(list(told.lines))
        return write(did, command)

    witness.about_to_send = written_after_the_last_was_answered
    host.start_instance(TARGET, WEB, LINK, GENERATION, witness)
    host.retire_instance(WEB, witness)
    dids = [f"make {WEB}", f"start {WEB}", f"stop {WEB}", f"remove {WEB}"]
    assert told.lines == [(did, Said.CHANGED) for did in dids]
    assert [told.sent_before[did] for did in dids] == [["not made"], [], ["start"], ["start", "stop within 30 s"]]
    assert answered_when_written == [told.lines[:place] for place in range(len(dids))]
    given = given_to(told.docker, WEB)
    assert not [value for value in given.values() for did, _ in told.lines if value in did]


def test_a_command_whose_line_cannot_be_written_is_not_sent() -> None:
    told = Told()
    host, part = a_part_running(told)

    def not_written(did: str, _command: Command) -> Answered:
        raise Interrupted(f"The attempt has ended in the record, so it was not written and not done: {did}")

    with pytest.raises(Interrupted):
        host.retire_instance(WEB, Witness(about_to_send=not_written))
    with pytest.raises(Interrupted):
        host.start_instance(TARGET, "shop-testing-bbbbbbbbbbbb", None, GENERATION, Witness(about_to_send=not_written))
    assert (part.calls, list(told.docker.containers.by_name)) == (["start"], [WEB])


def commands(kept: Kept, verb: str | None = None) -> list[Event]:
    return [each for each in kept.events if each.command is not None and verb in (None, each.command.verb)]


def test_a_command_is_kept_apart_from_a_change_of_the_route_and_only_the_attempt_that_is_open_answers_its_line(
    record: Record,
) -> None:
    request = record.write(MOVE_B, CALLER, None)
    first = record.take_up(request.id)
    assert first is not None
    stop = Command(verb="stop", container="a")
    line = record.about_to(first.attempt, "stop a", stop)
    route = record.about_to(first.attempt, "send x to a", Sent(web_address="x", to="a"))
    record.complete(first.attempt, line, Reply(made=True))
    record.complete(first.attempt, route, Reply(made=True))
    command, change = kept_of(record, request.id).events
    assert (command.command, command.action, command.is_own, command.is_credited) == (stop, None, True, False)
    assert (change.command, change.action, change.is_own, change.is_credited) == (None, Sent(web_address="x", to="a"), True, True)
    assert record.take_up(request.id) is not None
    with pytest.raises(Interrupted):
        record.about_to(first.attempt, "remove a", Command(verb="remove", container="a"))


def test_a_removal_docker_refuses_after_a_stop_it_answered_shows_both_lines_and_the_request_does_not_say_nothing_was_changed(
    record: Record,
) -> None:
    worker, _, proxy = serving_a(record)
    proxy.host.place(BUILD_A, "stray")
    proxy.host.commands_refused = {"remove stray"}
    request = record.write(MOVE_A, CALLER, None)
    assert worker.work_next() is True
    kept = kept_of(record, request.id)
    assert (kept.stands, sorted(proxy.host.instances)) == (Stands.FAILED, ["a", "stray"])
    assert lines_of(kept) == ["stop stray: done", "remove stray: refused", "retire stray, refused"]
    assert kept.error is not None and "Nothing was changed" not in kept.error
    assert "409 Conflict Before that, this call stopped stray, and those changes stay." in kept.error


# The line is in the record, and Launchplane dies before the command it was written for is sent.
def killed_once_it_wrote(monkeypatch: pytest.MonkeyPatch, record: Record, is_the_line: Callable[[str], bool]) -> None:
    about_to = record.about_to

    def write_and_die(attempt: int, did: str, action: Sent | Command) -> int:
        line = about_to(attempt, did, action)
        if is_the_line(did):
            monkeypatch.setattr(record, "about_to", about_to)
            raise LaunchplaneKilled
        return line

    monkeypatch.setattr(record, "about_to", write_and_die)


def is_the_start_of_a_background_service(did: str) -> bool:
    return did.startswith("start ") and did.endswith("-background-service")


def test_a_start_whose_line_was_written_and_that_was_never_sent_is_not_credited_when_the_next_attempt_starts_the_part_once(
    record: Record, monkeypatch: pytest.MonkeyPatch
) -> None:
    proxy = lane_brought_up_with_a()
    request = record.write(MOVE_B, CALLER, None)
    killed_once_it_wrote(monkeypatch, record, is_the_start_of_a_background_service)
    with pytest.raises(LaunchplaneKilled):
        worker_on(proxy, record).work_next()
    assert worker_on(proxy, record).work_next() is True
    kept = kept_of(record, request.id)
    never_sent, sent = commands(kept, "start")[-2:]
    assert kept.answer is not None and never_sent.command == sent.command
    started = [action for action in proxy.host.actions if action.startswith("start background service")]
    assert (kept.stands, len(started), kept.answer["did"].count(sent.did)) == (Stands.DONE, 1, 1)
    assert (never_sent.attempt, never_sent.is_own, sent.attempt, sent.is_own) == (1, False, 2, True)
    assert line_of(never_sent, attempt_is_open=False) == f"{sent.did}: may have been sent, no answer"
    assert line_of(sent, attempt_is_open=False) == f"{sent.did}: done"


def test_a_make_with_no_answer_is_the_requests_own_once_a_reading_shows_its_container_under_the_requests_generation(
    record: Record, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker = serving_a(record)[0]
    complete = record.complete

    def killed_before_the_answer_is_written(attempt: int, line: int, outcome: Reply, error: str | None = None) -> None:
        monkeypatch.setattr(record, "complete", complete)
        raise LaunchplaneKilled(attempt, line, outcome, error)

    monkeypatch.setattr(record, "complete", killed_before_the_answer_is_written)
    request = record.write(MOVE_B, CALLER, None)
    with pytest.raises(LaunchplaneKilled):
        worker.work_next()
    assert worker.work_next() is True
    kept = kept_of(record, request.id)
    unanswered, stopped = kept.events[:2]
    assert kept.answer is not None and unanswered.outcome is not None and unanswered.command is not None
    assert (unanswered.did, unanswered.attempt, unanswered.outcome.made, unanswered.is_own) == ("make site-testing-0", 1, None, True)
    assert line_of(unanswered, attempt_is_open=False) == (
        "make site-testing-0: may have been sent, no answer, then read as made by this request"
    )
    assert line_of(stopped, attempt_is_open=False) == "stop site-testing-0: found already as asked"
    assert kept.answer["did"][:2] == ["make site-testing-0", "remove site-testing-0"]


def killed_between_a_make_and_its_start(record: Record, monkeypatch: pytest.MonkeyPatch) -> tuple[PretendProxy, int]:
    proxy = lane_brought_up_with_a()
    request = record.write(MOVE_B, CALLER, None)
    killed_once_it_wrote(monkeypatch, record, lambda did: did.startswith("start "))
    with pytest.raises(LaunchplaneKilled):
        worker_on(proxy, record).work_next()
    return proxy, request.id


def killed_before_the_start_of_a_background_service_is_sent(
    record: Record, monkeypatch: pytest.MonkeyPatch
) -> tuple[PretendProxy, int]:
    proxy = lane_brought_up_with_a()
    request = record.write(MOVE_B, CALLER, None)
    killed_once_it_wrote(monkeypatch, record, is_the_start_of_a_background_service)
    with pytest.raises(LaunchplaneKilled):
        worker_on(proxy, record).work_next()
    return proxy, request.id


KILLED: dict[str, Callable[[Record, pytest.MonkeyPatch], tuple[PretendProxy, int]]] = {
    "killed between a make and its start": killed_between_a_make_and_its_start,
    "killed before the start of a background service is sent": killed_before_the_start_of_a_background_service_is_sent,
    **{name: lambda record, _monkeypatch, begun=begun: begun(record) for name, begun in INTERRUPTED.items()},
}


@pytest.mark.parametrize("interrupted", KILLED.values(), ids=KILLED.keys())
def test_with_every_line_of_a_command_and_of_a_route_change_deleted_a_fresh_worker_makes_the_same_calls_in_the_same_order(
    monkeypatch: pytest.MonkeyPatch, interrupted: Callable[[Record, pytest.MonkeyPatch], tuple[PretendProxy, int]]
) -> None:
    def carried_from_the_interruption(*, erased: bool) -> tuple[list[str], Kept, object]:
        # A generation of its own: the lane was brought up under the first.
        generations = count(2)
        monkeypatch.setattr("tests.pretend_record.new_generation", lambda: f"01a1ef36-0000-7000-8000-{next(generations):012}")
        record = PretendRecord()
        proxy, request = interrupted(record, monkeypatch)
        kept = kept_of(record, request)
        lines = [each for each in kept.events if (each.action or each.command) is not None]
        assert lines, "the interruption left no line to delete"
        if erased:
            record.change(request, events=[each for each in kept.events if each not in lines])
        return carried_on(record, proxy, request)

    kept, erased = carried_from_the_interruption(erased=False), carried_from_the_interruption(erased=True)
    own, own_erased = ([each.did for each in ended.events if each.is_own] for ended in (kept[1], erased[1]))
    assert kept[0] and (kept[0], kept[2], kept[1].stands) == (erased[0], erased[2], Stands.DONE)
    assert (erased[1].stands, own != own_erased) == (Stands.DONE, True)


LINE: dict[str, Any] = {"id": 1, "at": "2026-11-22T10:00:00Z", "attempt": 1, "did": "stop a", "error": None}
STOP = {"verb": "stop", "container": "a"}
LINES = {
    "about to stop a": (LINE | {"action": STOP}, True),
    "stop a: may have been sent, no answer": (LINE | {"action": STOP}, False),
    "stop a: done": (LINE | {"command": STOP, "outcome": {"made": True}}, False),
    "stop a: found already as asked": (LINE | {"action": STOP, "outcome": {"made": False, "already": True}}, False),
    "stop a: refused": (LINE | {"action": STOP, "outcome": {"made": False}, "error": "409 Conflict"}, True),
}


@pytest.mark.parametrize(("shown", "line"), LINES.items(), ids=LINES.keys())
def test_the_page_says_how_far_a_command_for_a_part_is_known_to_have_got_as_it_does_for_a_change_of_the_route(
    shown: str, line: tuple[dict[str, Any], bool]
) -> None:
    assert line_of(Event.model_validate(line[0]), line[1]) == shown
