import threading
from collections.abc import Generator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from launchplane.record import (
    ASKED_AGAIN,
    ENDED,
    GIVEN_UP,
    MINUTES_A_CANCELLED_REQUEST_IS_SHOWN,
    MOST_ATTEMPTS,
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
    Request,
    Stands,
    Unconfirmed,
    key_digest,
    key_reused,
    lane_waits_on_a_person,
    needs_a_person,
    said,
)
from launchplane.secret_boundary import JsonValue
from launchplane.steps import PutBack
from tests.given_secrets import GivenSecrets

UNFINISHED = (Stands.WAITING, Stands.WORKING, Stands.NEEDS_A_PERSON)


def now() -> datetime:
    return datetime.now(UTC)


@dataclass
class PretendRecord(Record):
    kept: dict[int, Kept] = field(default_factory=dict[int, Kept])
    open_attempts: dict[int, int] = field(default_factory=dict[int, int])
    turns: dict[tuple[str, str], object] = field(default_factory=dict[tuple[str, str], object])
    attempts_begun: int = 0
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
        if idempotency_key is None:
            unfinished = (each for each in self.every() if each.stands in UNFINISHED[:2] and each.cancel_asked_at is None)
            return next((each for each in unfinished if each.asked() == asked), None)
        same_key = self.kept.get(self.keys.get(key_digest(idempotency_key) or "", 0))
        if same_key is not None and same_key.asked() != asked:
            raise key_reused(same_key)
        return same_key

    def write_new(self, asked: Asked, asked_by: str, idempotency_key: str | None) -> Request:
        self.gate.refuse(*said(asked), asked_by)
        new = Kept(
            **asked.model_dump(),
            id=len(self.kept) + 1,
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
        before = [each.id for each in self.every() if (each.product, each.lane, each.id < new.id) == (new.product, new.lane, True)]
        self.cancel_each(before, f"request {new.id}")
        if not self.confirms_writes:
            raise Unconfirmed("Launchplane's record could not be reached while writing a request: the connection was lost.")
        return new

    def write(self, asked: Asked, asked_by: str, idempotency_key: str | None) -> Request:
        if not self.reachable:
            raise Unconfirmed("Launchplane's record could not be reached while writing a request: connection refused.")
        self.gate.refuse(*said(asked), asked_by)
        with self.writing:
            same = self.same_as_written(asked, idempotency_key)
            if same is not None:
                return same
            of_lane = [each for each in self.every() if (each.product, each.lane) == (asked.product, asked.lane)]
            stuck = next((each.id for each in of_lane if each.stands is Stands.NEEDS_A_PERSON), None)
            if stuck is not None:
                raise lane_waits_on_a_person(asked, stuck)
            return self.write_new(asked, asked_by, idempotency_key)

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

    def recently_cancelled(self) -> list[Request]:
        self.reach("reading the requests that were cancelled")
        since = now() - timedelta(minutes=MINUTES_A_CANCELLED_REQUEST_IS_SHOWN)
        return [each for each in self.every() if each.stands is Stands.CANCELLED and each.ended_at and each.ended_at > since]

    def next_up(self) -> list[Request]:
        self.reach("looking for the next requests")
        stuck = {(each.product, each.lane) for each in self.every() if each.stands is Stands.NEEDS_A_PERSON}
        oldest: dict[tuple[str, str], Request] = {}
        for each in self.every():
            if each.stands in UNFINISHED[:2] and (each.product, each.lane) not in stuck:
                oldest.setdefault((each.product, each.lane), each)
        return sorted(oldest.values(), key=lambda each: each.id)

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

    def take_up(self, request: int) -> int | None:
        self.reach("taking up a request")
        with self.writing:
            found = self.kept.get(request)
            if found is None or found.stands not in UNFINISHED[:2]:
                return None
            interrupted = [
                each if each.ended is not None else each.model_copy(update={"ended": "interrupted", "ended_at": now()})
                for each in found.attempts
            ]
            self.open_attempts = {attempt: of for attempt, of in self.open_attempts.items() if of != request}
            if len(interrupted) >= MOST_ATTEMPTS:
                self.change(request, attempts=interrupted, stands=Stands.NEEDS_A_PERSON, **self.cleaned(error=needs_a_person(found)))
                return None
            began = Attempt(number=len(interrupted) + 1, began_at=now(), ended_at=None, ended=None)
            self.change(request, attempts=[*interrupted, began], stands=Stands.WORKING)
            self.attempts_begun += 1
            self.open_attempts[self.attempts_begun] = request
            return self.attempts_begun

    def note(self, attempt: int, did: str, error: str | None) -> None:
        self.reach("writing what was done")
        with self.writing:
            request = self.open_attempts.get(attempt)
            if request is None:
                raise Interrupted(f"Attempt {attempt} has ended in the record, so what it did next was not written: {did}")
            found = self.kept[request]
            event = Event(at=now(), attempt=len(found.attempts), **self.cleaned(did=did, error=error))
            self.change(request, events=[*found.events, event])

    def end(self, attempt: int, stands: Stands, answer: dict[str, Any] | None, error: str | None) -> None:
        self.reach("writing how a request ended")
        with self.writing:
            request = self.open_attempts.pop(attempt, None)
            if request is None:
                raise Interrupted(f"Attempt {attempt} has ended in the record, so its answer was not written.")
            attempts = self.kept[request].attempts
            ended = attempts[-1].model_copy(update={"ended": stands.value, "ended_at": now()})
            kept = self.cleaned(answer=answer, error=error)
            self.change(request, attempts=[*attempts[:-1], ended], stands=stands, ended_at=now(), **kept)

    def answered_by_a_person(self, request: int, how: str) -> Request | None:
        found = self.kept.get(request)
        if found is None or found.stands is not Stands.NEEDS_A_PERSON:
            return None
        return self.change(request, stands=Stands.FAILED, ended_at=now(), error=f"{found.error}{how}")

    def give_up(self, request: int) -> Request | None:
        self.reach("giving a request up")
        with self.writing:
            return self.answered_by_a_person(request, GIVEN_UP)

    def retry(self, request: int, asked_by: str) -> Request | None:
        self.reach("asking for a request again")
        with self.writing:
            found = self.kept.get(request)
            if found is None or found.stands is not Stands.NEEDS_A_PERSON:
                return None
            again = self.write_new(found.asked(), asked_by, None)
            self.answered_by_a_person(request, ASKED_AGAIN.format(again=again.id))
            return again

    def cancel_each(self, requests: list[int], by: str) -> None:
        self.gate.refuse(by)
        for request in requests:
            found = self.kept.get(request)
            if found is None or found.stands not in UNFINISHED[:2] or found.cancel_asked_at is not None:
                continue
            mark = {"cancel_asked_at": now(), "cancel_asked_by": by}
            if found.stands is Stands.WAITING:
                self.change(request, **mark, stands=Stands.CANCELLED, ended_at=now(), answer=PutBack().model_dump())
            else:
                self.change(request, **mark)

    def cancel(self, request: int, by: str) -> Request | None:
        self.reach("cancelling a request")
        with self.writing:
            self.cancel_each([request], by)
            return self.kept.get(request)

    def cancel_wanted(self, request: int) -> bool:
        self.reach("reading whether a request is to be cancelled")
        found = self.kept.get(request)
        return found is not None and found.cancel_asked_at is not None and found.cancel_refused is None

    def refuse_cancel(self, attempt: int, why: str) -> None:
        self.reach("writing why a request was not cancelled")
        with self.writing:
            request = self.open_attempts.get(attempt)
            if request is None:
                raise Interrupted(f"Attempt {attempt} has ended in the record, so why it was not cancelled was not written.")
            self.change(request, **self.cleaned(cancel_refused=why))


# What a lost connection does: every turn is free, and its holder has not been told.
def lose_every_turn(record: Record) -> None:
    if isinstance(record, PretendRecord):
        record.turns.clear()
    if isinstance(record, PostgresRecord):
        with record.session("ending every connection that holds a turn") as session:
            session.execute("select pg_terminate_backend(pid) from pg_locks where locktype = 'advisory'")
