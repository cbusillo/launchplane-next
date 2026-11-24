import threading
from collections.abc import Generator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from launchplane.instance import Verdict, new_generation
from launchplane.judge import Work
from launchplane.record import (
    ENDED,
    GIVEN_UP,
    MINUTES_AN_ENDED_REQUEST_IS_SHOWN,
    Asked,
    Attempt,
    Event,
    Gate,
    Held,
    Interrupted,
    Kept,
    PostgresRecord,
    Record,
    RecordError,
    Reply,
    Request,
    Sent,
    Stands,
    Taken,
    Unconfirmed,
    key_digest,
    key_reused,
    said,
)
from launchplane.secret_boundary import JsonValue
from launchplane.steps import Command, PutBack
from tests.given_secrets import GivenSecrets

UNFINISHED = (Stands.WAITING, Stands.WORKING)


def now() -> datetime:
    return datetime.now(UTC)


@dataclass
class PretendRecord(Record):
    kept: dict[int, Kept] = field(default_factory=dict[int, Kept])
    open_attempts: dict[int, int] = field(default_factory=dict[int, int])
    turns: dict[tuple[str, str], object] = field(default_factory=dict[tuple[str, str], object])
    attempts_begun: int = 0
    lines_written: int = 0
    reachable: bool = True
    gate: Gate = field(default_factory=lambda: Gate(GivenSecrets()))
    confirms_writes: bool = True
    keys: dict[str, int] = field(default_factory=dict[str, int])
    writing: threading.RLock = field(default_factory=threading.RLock)

    def close(self) -> None:
        pass

    def reach(self, doing: str) -> None:
        if not self.reachable:
            raise RecordError(f"Launchplane's record could not be reached while {doing}: connection refused.")

    def cleaned(self, **values: JsonValue) -> dict[str, Any]:
        return {name: self.gate.clean(value) for name, value in values.items()}

    # A copy, so one thread reads every request while another writes a new one.
    def every(self) -> list[Kept]:
        return list(self.kept.values())

    def change(self, request: int, **changes: object) -> Kept:
        self.kept[request] = self.kept[request].model_copy(update=changes)
        return self.kept[request]

    def same_as_written(self, asked: Asked, idempotency_key: str | None) -> Request | None:
        same_key = self.kept.get(self.keys.get(key_digest(idempotency_key) or "", 0))
        if same_key is not None and same_key.asked().named() != asked.named():
            raise key_reused(same_key)
        unfinished = (each for each in self.every() if each.stands in UNFINISHED and each.cancel_asked_at is None)
        same = same_key or next((each for each in unfinished if each.asked() == asked), None)
        if same is not None and idempotency_key is not None:
            self.keys[key_digest(idempotency_key) or ""] = same.id
        return same

    def write_new(self, asked: Asked, asked_by: str, idempotency_key: str | None) -> Request:
        self.gate.refuse(*said(asked), asked_by)
        new = Kept(
            **asked.model_dump(),
            id=len(self.kept) + 1,
            generation=new_generation(),
            asked_by=asked_by,
            asked_at=now(),
            stands=Stands.WAITING,
            ended_at=None,
            answer=None,
            error=None,
            attempts=[],
            events=[],
        )
        self.kept[new.id] = new
        if idempotency_key is not None:
            self.keys[key_digest(idempotency_key) or ""] = new.id
        for each in self.every():
            if (each.product, each.lane, each.stands, each.id < new.id) == (new.product, new.lane, Stands.WAITING, True):
                self.change(each.id, stands=Stands.REPLACED, ended_at=now(), replaced_by=new.id)
        if not self.confirms_writes:
            raise Unconfirmed("Launchplane's record could not be reached while writing a request: the connection was lost.")
        return new

    def kept_for(self, asked: Asked, idempotency_key: str) -> Request | None:
        if not self.reachable:
            raise Unconfirmed("Launchplane's record could not be reached while reading the request of a key: connection refused.")
        same_key = self.kept.get(self.keys.get(key_digest(idempotency_key) or "", 0))
        if same_key is not None and same_key.asked().named() != asked.named():
            raise key_reused(same_key)
        return same_key

    def write(self, asked: Asked, asked_by: str, idempotency_key: str | None) -> Request:
        if not self.reachable:
            raise Unconfirmed("Launchplane's record could not be reached while writing a request: connection refused.")
        self.gate.refuse(*said(asked), asked_by)
        with self.writing:
            same = self.same_as_written(asked, idempotency_key)
            return same or self.write_new(asked, asked_by, idempotency_key)

    def read(self, request: int) -> Kept | None:
        self.reach("reading a request")
        return self.kept.get(request)

    def unfinished(self) -> list[Kept]:
        self.reach("reading the requests not finished")
        return [each for each in self.every() if each.stands in UNFINISHED]

    def recently_ended(self) -> list[Request]:
        self.reach("reading the requests that ended")
        ended = list[Request](each for each in self.every() if each.stands in ENDED)
        return sorted(ended, key=lambda each: each.id, reverse=True)[:20]

    def ended_lately(self) -> list[Kept]:
        self.reach("reading the requests that ended a short while ago")
        since = now() - timedelta(minutes=MINUTES_AN_ENDED_REQUEST_IS_SHOWN)
        ended = [each for each in self.every() if each.stands in ENDED and each.ended_at and each.ended_at > since]
        last: dict[tuple[str, str], Kept] = {}
        for each in sorted(ended, key=lambda each: (each.ended_at, each.id)):
            if each.stands is not Stands.REPLACED:
                last[each.product, each.lane] = each
        shown = (each.cancel_asked_at is not None or each.stands is Stands.REPLACED or each in last.values() for each in ended)
        return [each for each, is_shown in zip(ended, shown, strict=True) if is_shown]

    def next_up(self) -> list[Request]:
        self.reach("looking for the next requests")
        oldest: dict[tuple[str, str], Request] = {}
        for each in self.every():
            if each.stands in UNFINISHED:
                oldest.setdefault((each.product, each.lane), each)
        return sorted(oldest.values(), key=lambda request: request.id)

    def turn(self, product: str, lane: str) -> AbstractContextManager[Held | None]:
        return self.turn_held(product, lane)

    @contextmanager
    def turn_held(self, product: str, lane: str) -> Generator[Held | None]:
        self.reach("taking a lane's turn")
        holder = object()
        with self.writing:
            ours = self.turns.setdefault((product, lane), holder) is holder
        if not ours:
            yield None
            return
        try:
            yield lambda: self.turns.get((product, lane)) is holder
        finally:
            if self.turns.get((product, lane)) is holder:
                del self.turns[product, lane]

    def take_up(self, request: int) -> Taken | None:
        self.reach("taking up a request")
        with self.writing:
            found = self.kept.get(request)
            if found is None or found.stands not in UNFINISHED:
                return None
            interrupted = [
                each if each.ended is not None else each.model_copy(update={"ended": "interrupted", "ended_at": now()})
                for each in found.attempts
            ]
            self.open_attempts = {attempt: of for attempt, of in self.open_attempts.items() if of != request}
            self.attempts_begun += 1
            began = Attempt(id=self.attempts_begun, number=len(interrupted) + 1, began_at=now(), ended_at=None, ended=None)
            self.change(request, attempts=[*interrupted, began], stands=Stands.WORKING)
            self.open_attempts[self.attempts_begun] = request
            return Taken(self.attempts_begun, again=found.stands is Stands.WORKING)

    def leave_open(self, attempt: int, why: str) -> None:
        self.reach("writing why a request is not finished")
        with self.writing:
            request = self.open_attempts.pop(attempt, None)
            if request is None:
                raise Interrupted(f"Attempt {attempt} has ended in the record, so why it is not finished was not written.")
            attempts = self.kept[request].attempts
            left = attempts[-1].model_copy(update={"ended": "unfinished", "ended_at": now()})
            self.change(request, attempts=[*attempts[:-1], left], **self.cleaned(error=why))

    def note(self, attempt: int, did: str, error: str | None) -> None:
        self.reach("writing what was done")
        with self.writing:
            request = self.open_attempts.get(attempt)
            if request is None:
                raise Interrupted(f"Attempt {attempt} has ended in the record, so what it did next was not written: {did}")
            self.written(request, self.cleaned(did=did, error=error))

    def written(self, request: int, line: dict[str, Any]) -> int:
        found = self.kept[request]
        self.lines_written += 1
        event = Event.model_validate({"id": self.lines_written, "at": now(), "attempt": len(found.attempts), "error": None} | line)
        self.change(request, events=[*found.events, event])
        return event.id

    def about_to(self, attempt: int, did: str, action: Sent | Command) -> int:
        self.reach("writing what is about to be done")
        with self.writing:
            request = self.open_attempts.get(attempt)
            if request is None:
                raise Interrupted(f"Attempt {attempt} has ended in the record, so it was not written and not done: {did}")
            return self.written(request, self.cleaned(did=did, action=action.model_dump()))

    def complete(self, attempt: int, line: int, outcome: Reply, error: str | None = None) -> None:
        self.reach("writing what came of a change")
        with self.writing:
            request = self.open_attempts.get(attempt)
            events = [] if request is None else self.kept[request].events
            if request is None or line not in {each.id for each in events}:
                raise Interrupted(f"Attempt {attempt} has ended in the record, so what came of a change was not written.")
            new = self.cleaned(outcome=outcome.model_dump(mode="json", exclude_unset=True), error=error)
            completed = [each if each.id != line else self.completed(each, new["outcome"], new["error"]) for each in events]
            self.change(request, events=completed)

    @staticmethod
    def completed(event: Event, outcome: dict[str, Any], error: str | None) -> Event:
        known = ({} if event.outcome is None else event.outcome.model_dump(mode="json", exclude_unset=True)) | outcome
        return Event.model_validate(event.model_dump() | {"id": event.id, "outcome": known, "error": error or event.error})

    def end(
        self, attempt: int, stands: Stands, answer: dict[str, Any] | None, error: str | None, *, decided: Verdict | None = None
    ) -> bool:
        self.reach("writing how a request ended")
        with self.writing:
            request = self.open_attempts.get(attempt)
            if request is None:
                raise Interrupted(f"Attempt {attempt} has ended in the record, so its answer was not written.")
            found = self.kept[request]
            marked = found.work is Work.MOVE and found.cancel_asked_at is not None
            if marked and decided not in (Verdict.ABANDONED, Verdict.COMMITTED):
                return False
            del self.open_attempts[attempt]
            attempts = self.kept[request].attempts
            ended = attempts[-1].model_copy(update={"ended": stands.value, "ended_at": now()})
            kept = self.cleaned(answer=answer, error=error)
            verdict = decided if found.work is Work.MOVE else None
            self.change(request, attempts=[*attempts[:-1], ended], stands=stands, ended_at=now(), release_verdict=verdict, **kept)
            return True

    def give_up(self, request: int, last: int) -> Request | None:
        self.reach("giving a request up")
        with self.writing:
            found = self.kept.get(request)
            resting = found is not None and found.stands is Stands.WORKING and request not in self.open_attempts.values()
            if found is None or not resting or found.cancel_asked_at is not None or found.attempts[-1].id != last:
                return None
            return self.change(request, stands=Stands.FAILED, ended_at=now(), error=" ".join(filter(None, [found.error, GIVEN_UP])))

    def cancel(self, request: int, by: str) -> Request | None:
        self.reach("cancelling a request")
        self.gate.refuse(by)
        with self.writing:
            found = self.kept.get(request)
            if found is None or found.stands not in UNFINISHED or found.cancel_asked_at is not None:
                return found
            mark = {"cancel_asked_at": now(), "cancel_asked_by": by}
            if found.stands is Stands.WAITING:
                return self.change(request, **mark, stands=Stands.CANCELLED, ended_at=now(), answer=PutBack().model_dump())
            return self.change(request, **mark)

    def cancel_asked(self, request: int) -> bool:
        self.reach("reading whether a cancel was asked of a request")
        found = self.kept.get(request)
        return found is not None and found.cancel_asked_at is not None

    def refuse_cancel(self, request: int, why: str) -> None:
        self.reach("writing why a request was not cancelled")
        with self.writing:
            if request in self.kept and self.kept[request].cancel_refused is None:
                self.change(request, **self.cleaned(cancel_refused=why))


# The attempt a request is given when it is taken up, for a test that goes on as its worker.
def begun(record: Record, request: int) -> int:
    taken = record.take_up(request)
    assert taken is not None
    return taken.attempt


# What a lost connection does: every turn is free, and its holder has not been told.
def lose_every_turn(record: Record) -> None:
    if isinstance(record, PretendRecord):
        record.turns.clear()
    if isinstance(record, PostgresRecord):
        with record.session("ending every connection that holds a turn") as session:
            session.execute("select pg_terminate_backend(pid) from pg_locks where locktype = 'advisory'")
