import threading
from collections.abc import Generator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from launchplane.record import (
    ASKED_AGAIN,
    ENDED,
    GIVEN_UP,
    MOST_ATTEMPTS,
    Asked,
    Attempt,
    Event,
    Interrupted,
    Kept,
    Record,
    RecordError,
    Request,
    Stands,
    key_reused,
    lane_waits_on_a_person,
    needs_a_person,
)

UNFINISHED = (Stands.WAITING, Stands.WORKING, Stands.NEEDS_A_PERSON)


def now() -> datetime:
    return datetime.now(UTC)


@dataclass
class PretendRecord(Record):
    kept: dict[int, Kept] = field(default_factory=dict[int, Kept])
    open_attempts: dict[int, int] = field(default_factory=dict[int, int])
    turns: set[tuple[str, str]] = field(default_factory=set[tuple[str, str]])
    attempts_begun: int = 0
    reachable: bool = True
    writing: threading.RLock = field(default_factory=threading.RLock)

    def close(self) -> None:
        pass

    def reach(self, doing: str) -> None:
        if not self.reachable:
            raise RecordError(f"Launchplane's record could not be reached while {doing}: connection refused")

    def change(self, request: int, **changes: object) -> Kept:
        self.kept[request] = self.kept[request].model_copy(update=changes)
        return self.kept[request]

    def same_as_written(self, asked: Asked, idempotency_key: str | None) -> Request | None:
        if idempotency_key is None:
            return next((each for each in self.kept.values() if each.asked() == asked and each.stands in UNFINISHED[:2]), None)
        same_key = next((each for each in self.kept.values() if each.idempotency_key == idempotency_key), None)
        if same_key is not None and same_key.asked() != asked:
            raise key_reused(idempotency_key, same_key)
        return same_key

    def write_new(self, asked: Asked, asked_by: str, idempotency_key: str | None) -> Request:
        new = Kept(
            **asked.model_dump(),
            id=len(self.kept) + 1,
            asked_by=asked_by,
            asked_at=now(),
            idempotency_key=idempotency_key,
            stands=Stands.WAITING,
            ended_at=None,
            answer=None,
            error=None,
            attempts=[],
            events=[],
        )
        self.kept[new.id] = new
        return new

    def write(self, asked: Asked, asked_by: str, idempotency_key: str | None) -> Request:
        self.reach("writing a request")
        with self.writing:
            same = self.same_as_written(asked, idempotency_key)
            if same is not None:
                return same
            of_lane = [each for each in self.kept.values() if (each.product, each.lane) == (asked.product, asked.lane)]
            stuck = next((each.id for each in of_lane if each.stands is Stands.NEEDS_A_PERSON), None)
            if stuck is not None:
                raise lane_waits_on_a_person(asked, stuck)
            return self.write_new(asked, asked_by, idempotency_key)

    def read(self, request: int) -> Kept | None:
        self.reach("reading a request")
        return self.kept.get(request)

    def unfinished(self) -> list[Kept]:
        self.reach("reading the requests not finished")
        return [each for each in self.kept.values() if each.stands in UNFINISHED]

    def recently_ended(self) -> list[Request]:
        self.reach("reading the requests that ended")
        ended = list[Request](each for each in self.kept.values() if each.stands in ENDED)
        return sorted(ended, key=lambda each: each.id, reverse=True)[:20]

    def next_up(self) -> Request | None:
        self.reach("looking for the next request")
        stuck = {(each.product, each.lane) for each in self.kept.values() if each.stands is Stands.NEEDS_A_PERSON}
        waiting = (each for each in self.kept.values() if each.stands in UNFINISHED[:2] and (each.product, each.lane) not in stuck)
        return next(waiting, None)

    def turn(self, product: str, lane: str) -> AbstractContextManager[bool]:
        return self.turn_held(product, lane)

    @contextmanager
    def turn_held(self, product: str, lane: str) -> Generator[bool]:
        self.reach("taking a lane's turn")
        ours = (product, lane) not in self.turns
        self.turns.add((product, lane))
        try:
            yield ours
        finally:
            if ours:
                self.turns.discard((product, lane))

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
                self.change(request, attempts=interrupted, stands=Stands.NEEDS_A_PERSON, error=needs_a_person(found))
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
            event = Event(at=now(), attempt=len(found.attempts), did=did, error=error)
            self.change(request, events=[*found.events, event])

    def end(self, attempt: int, stands: Stands, answer: dict[str, Any] | None, error: str | None) -> None:
        self.reach("writing how a request ended")
        with self.writing:
            request = self.open_attempts.pop(attempt, None)
            if request is None:
                raise Interrupted(f"Attempt {attempt} has ended in the record, so its answer was not written.")
            attempts = self.kept[request].attempts
            ended = attempts[-1].model_copy(update={"ended": stands.value, "ended_at": now()})
            self.change(request, attempts=[*attempts[:-1], ended], stands=stands, ended_at=now(), answer=answer, error=error)

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
