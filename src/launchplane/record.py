import hashlib
import os
from collections.abc import Callable, Generator, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractContextManager, closing, contextmanager
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Final, LiteralString, NamedTuple, Protocol

import psycopg
from psycopg.rows import DictRow, dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, ConfigDict, Field

from launchplane.instance import Verdict, new_generation
from launchplane.judge import Work
from launchplane.secret_boundary import JsonValue, Secrets, carries_a_secret, cleaned_value
from launchplane.steps import PutBack
from launchplane.telling import Said, Seen

DATABASE = "postgresql:///launchplane?host=/var/run/postgresql"
SECONDS_TO_REACH_THE_DATABASE = 3.0
MOST_CONNECTIONS = 10
MOST_READINGS_AT_ONCE = 4
MINUTES_AN_ANSWERED_CANCEL_IS_SHOWN = 10

# Asked before each action on the host: whether the lane's turn is still held.
type Held = Callable[[], bool]


class RecordError(Exception):
    pass


# Writing a request may or may not have reached the record, so the request may be worked.
class Unconfirmed(RecordError):
    pass


class Refused(Exception):
    pass


class CarriesASecret(Exception):
    pass


# Another Launchplane has taken the request up since this attempt began, so this attempt writes nothing more.
class Interrupted(Exception):
    pass


# Values the gate has judged. A statement takes nothing else.
@dataclass(frozen=True)
class Passed:
    values: tuple[JsonValue, ...]


# The one place every value passes on its way into the record, which outlives the container that holds a secret. What a
# caller sent is refused when it carries a secret a container holds; what Launchplane writes itself is cleaned of it.
class Gate:
    def __init__(self, secrets: Secrets) -> None:
        self.secrets = secrets

    def refuse(self, *values: JsonValue) -> None:
        if carries_a_secret(list(values), self.secrets.secrets_seen()):
            raise CarriesASecret(
                "What was sent carries a secret Launchplane gave a lane, and the record keeps what was sent, so nothing was "
                "written. Nothing was changed."
            )

    def clean(self, value: JsonValue) -> JsonValue:
        return cleaned_value(value, self.secrets.secrets_seen())

    def sent(self, *values: JsonValue) -> Passed:
        self.refuse(*values)
        return Passed(values)

    def made(self, *values: JsonValue) -> Passed:
        return Passed(tuple(self.clean(value) for value in values))


class Session:
    def __init__(self, cursor: psycopg.Cursor[DictRow]) -> None:
        self.cursor = cursor

    def execute(self, query: LiteralString, passed: Passed | None = None) -> psycopg.Cursor[DictRow]:
        if passed is None:
            return self.cursor.execute(query)
        return self.cursor.execute(query, [Jsonb(value) if isinstance(value, dict) else value for value in passed.values])


class Stands(StrEnum):
    WAITING = "waiting"
    WORKING = "working"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"
    REPLACED = "replaced"


ENDED = (Stands.DONE, Stands.FAILED, Stands.CANCELLED, Stands.REPLACED)

ATTEMPTED = {
    Work.MOVE: "The move of {lane} of {product} to {build}",
    Work.RETIRE: "Retiring {lane} of {product}",
    Work.BEGIN_MAINTENANCE: "Putting into maintenance {lane} of {product}",
    Work.END_MAINTENANCE: "Taking out of maintenance {lane} of {product}",
}


class Asked(BaseModel):
    model_config = ConfigDict(frozen=True)

    product: str
    lane: str
    work: Work
    build: str | None = None
    delete_data_set: str | None = None

    def attempted(self) -> str:
        return ATTEMPTED[self.work].format(lane=self.lane, product=self.product, build=self.build)


class Request(Asked):
    id: int
    # The number of the release this request is: stamped on what it makes, and never given to another request.
    generation: str
    asked_by: str
    asked_at: datetime
    stands: Stands
    ended_at: datetime | None
    answer: dict[str, Any] | None
    error: str | None
    cancel_asked_at: datetime | None = None
    cancel_asked_by: str | None = None
    # Why a cancel that was asked was not carried out, once the worker has read the host and said so.
    cancel_refused: str | None = None
    # The newer request for the lane that took the waiting place from this one, which then ended with nothing done.
    replaced_by: int | None = None
    # What the gate answered for a move's release when the move ended. None is no move, or one ended before this was kept.
    release_verdict: Verdict | None = None

    def asked(self) -> Asked:
        return Asked.model_validate(self.model_dump(include=set(Asked.model_fields)))


class Attempt(BaseModel):
    # What the gate knows the attempt by. It is no part of an answer, which counts a request's attempts from one.
    id: int = Field(exclude=True)
    number: int
    began_at: datetime
    ended_at: datetime | None
    ended: str | None


# What a change line says before the change is sent: the one thing chosen, and whether it puts the lane back.
class Sent(BaseModel):
    verb: str
    subject: str
    back: bool = False
    # Of a change of the route: where it sends visitors, and where they were sent when it was chosen.
    to: str | None = None
    was: str | None = None


# What came of a change that was sent. `said` is the host's own answer and `seen` what a later attempt read of the host:
# two facts, and neither is made from the other. `changed` names what the host had changed, also before a refusal.
class Reply(BaseModel):
    said: Said | None = None
    at: datetime | None = None
    changed: list[str] = []
    seen: Seen | None = None
    seen_at: datetime | None = None


# One line of what a request did. A change has `action`, written before it was sent, and `outcome` once anything is known
# of it. A line with neither is a read, a wait or a check, or was written before changes were written down ahead.
class Event(BaseModel):
    id: int = Field(exclude=True)
    at: datetime
    attempt: int
    did: str
    error: str | None
    action: Sent | None = None
    outcome: Reply | None = None

    # Whether this is a change the request is known to have made: the host said so, or shows it with the request's mark.
    @property
    def is_credited(self) -> bool:
        reply = self.outcome
        return reply is not None and (reply.said is Said.CHANGED or reply.seen is Seen.OURS or bool(reply.changed))

    # Whether the change may have acted, or may yet, with nothing to put it down to this request.
    @property
    def is_unanswered(self) -> bool:
        return self.action is not None and not self.is_credited and (self.outcome is None or self.outcome.said is None)


class Kept(Request):
    attempts: list[Attempt]
    events: list[Event]


# An attempt begun. `again` says the request had one before it, which may have sent the host a change that has not run yet.
class Taken(NamedTuple):
    attempt: int
    again: bool


class Record(Protocol):
    def write(self, asked: Asked, asked_by: str, idempotency_key: str | None) -> Request: ...
    def read(self, request: int) -> Kept | None: ...
    def unfinished(self) -> list[Kept]: ...
    def recently_ended(self) -> list[Request]: ...
    def cancels_answered(self) -> list[Request]: ...
    def next_up(self) -> list[Request]: ...
    def turn(self, product: str, lane: str) -> AbstractContextManager[Held | None]: ...
    def take_up(self, request: int) -> Taken | None: ...
    def leave_open(self, attempt: int, why: str) -> None: ...
    def note(self, attempt: int, did: str, error: str | None) -> None: ...
    def about_to(self, attempt: int, did: str, action: dict[str, JsonValue]) -> int: ...
    def complete(self, attempt: int, line: int, outcome: dict[str, JsonValue], error: str | None = None) -> None: ...
    def end(
        self, attempt: int, stands: Stands, answer: dict[str, Any] | None, error: str | None, *, decided: Verdict | None = None
    ) -> bool: ...
    def give_up(self, request: int, last: int) -> Request | None: ...
    def cancel(self, request: int, by: str) -> Request | None: ...
    def cancel_asked(self, request: int) -> bool: ...
    def refuse_cancel(self, request: int, why: str) -> None: ...


def database() -> str:
    return os.environ.get("LAUNCHPLANE_DATABASE", DATABASE)


# A caller never needs its key shown back, so only this is kept: a key that is a secret cannot be read from the record.
def key_digest(idempotency_key: str | None) -> str | None:
    return None if idempotency_key is None else hashlib.sha256(idempotency_key.encode()).hexdigest()


def how_cancelled(request: Request) -> str:
    answer = PutBack.model_validate(request.answer or {})
    did = (
        f"Done to put the lane back: {'; '.join(answer.put_back)}." if answer.put_back else "Nothing on the host had to be put back."
    )
    kept = f" It made the data set {', '.join(answer.data_sets_kept)}, which is kept." if answer.data_sets_kept else ""
    return f"{request.attempted()} was cancelled by {request.cancel_asked_by}. {did}{kept}"


def how_replaced(request: Request) -> str:
    return (
        f"{request.attempted()} was replaced by request {request.replaced_by} while it waited. It was never taken up, "
        "so nothing was done."
    )


# Why a request a cancel was asked of did not end as cancelled, from what was kept when it ended. None where nothing
# kept says more than how it stands.
def how_not_cancelled(request: Request) -> str | None:
    if request.cancel_refused is not None:
        return request.cancel_refused
    if request.cancel_asked_at is None or request.stands is not Stands.FAILED or request.work is not Work.MOVE:
        return None
    if request.release_verdict is Verdict.ABANDONED:
        return f"The cancel won at the gate, and its lane was not put back whole: {request.error}"
    if request.release_verdict is None:
        return f"{request.error} What the gate answered for its cancel was not kept."
    return None


GIVEN_UP = "A person gave it up, with its lane read as it was before the request or as the request would leave it."


# PyCharm has no schema to resolve these against. The tests run every statement against PostgreSQL.
# noinspection SqlResolve
class Sql:
    SAME_KEY: Final = "select request.* from request, request_key answered where (digest, request) = (%s, request.id)"
    KEEP_KEY: Final = "insert into request_key (digest, request) values (%s, %s)"
    SAME_UNFINISHED: Final = """
    select * from request
    where (product, lane, work) = (%s, %s, %s) and build is not distinct from %s and delete_data_set is not distinct from %s
        and stands in ('waiting', 'working') and cancel_asked_at is null
    order by id limit 1
    """
    WRITE: Final = """
    insert into request (product, lane, work, build, delete_data_set, asked_by, generation)
    values (%s, %s, %s, %s, %s, %s, %s) returning *
    """
    UNFINISHED: Final = "select * from request where stands in ('waiting', 'working') order by id"
    RECENTLY_ENDED: Final = """
    select * from request where stands in ('done', 'failed', 'cancelled', 'replaced') order by ended_at desc, id desc limit 20
    """
    CANCELS_ANSWERED: Final = """
    select * from request
    where (cancel_asked_at is not null and stands in ('done', 'failed', 'cancelled') or stands = 'replaced')
        and ended_at > now() - make_interval(mins => %s)
    order by id
    """
    ATTEMPTS: Final = "select id, request, began_at, ended_at, ended from attempt where request = any(%s) order by id"
    EVENTS: Final = "select id, request, attempt, at, did, error, action, outcome from event where request = any(%s) order by id"
    NEXT_UP: Final = """
    select * from (
        select distinct on (product, lane) * from request where stands in ('waiting', 'working') order by product, lane, id
    ) oldest_of_each_lane order by id
    """
    # One statement, so a request taken up at that moment is left to be worked: only one that still waits is replaced.
    # It leaves the waiting place before the new request is written, since the record refuses a lane two that wait.
    END_WAITING: Final = """
    update request set stands = 'replaced', ended_at = now() where (product, lane) = (%s, %s) and stands = 'waiting' returning id
    """
    REPLACED_BY: Final = "update request set replaced_by = %s where id = any(%s)"
    # One statement, so a request taken up during its cancel is either ended or marked, never neither.
    CANCEL: Final = """
    update request set
        stands = case stands when 'waiting' then 'cancelled' else stands end,
        ended_at = case stands when 'waiting' then now() end,
        answer = case stands when 'waiting' then %s end,
        cancel_asked_at = now(),
        cancel_asked_by = %s
    where id = %s and stands in ('waiting', 'working') and cancel_asked_at is null
    """
    CANCEL_ASKED: Final = "select cancel_asked_at is not null as asked from request where id = %s"
    REFUSE_CANCEL: Final = "update request set cancel_refused = %s where id = %s and cancel_refused is null"
    # Locks the request, so a cancel is either marked before this is read or finds the request ended. Only a move
    # answers every cancel at the gate, so only a move's ending waits for the gate to have decided the cancel.
    OPEN_ATTEMPT: Final = """
    select request.work = 'moving' and request.cancel_asked_at is not null as move_with_a_cancel
    from request, attempt where attempt.id = %s and attempt.ended is null and request.id = attempt.request
    for update of request
    """
    ONE_READING: Final = "set transaction isolation level repeatable read, read only"
    TURN_HELD: Final = "select count(*) as held from pg_locks where locktype = 'advisory' and pid = pg_backend_pid() and granted"
    TAKE_TURN: Final = "select pg_try_advisory_lock(hashtextextended(%s, 0)) as ours"
    INTERRUPT: Final = "update attempt set ended_at = now(), ended = 'interrupted' where request = %s and ended is null"
    RESTING: Final = "select max(id) as last, count(*) filter (where ended is null) as open from attempt where request = %s"
    LEAVE_OPEN: Final = """
    with open_still as (
        update attempt set ended_at = now(), ended = 'unfinished' where id = %s and ended is null returning request
    )
    update request set error = %s from open_still where request.id = open_still.request returning request.id
    """
    BEGIN_ATTEMPT: Final = """
    with taken as (update request set stands = 'working' where id = %s)
    insert into attempt (request) values (%s) returning id
    """
    NOTE: Final = """
    insert into event (request, attempt, did, error)
    select request, id, %s, %s from attempt where id = %s and ended is null returning id
    """
    # Committed before the change is sent. A change is never sent on a line that was not written.
    ABOUT_TO: Final = """
    insert into event (request, attempt, did, action)
    select request, id, %s, %s from attempt where id = %s and ended is null returning id
    """
    # Only the attempt that is open writes, and what is known of a line is added to, never replaced.
    COMPLETE: Final = """
    update event set outcome = nullif(coalesce(event.outcome, '{}') || %s, '{}'), error = coalesce(%s, event.error)
    from attempt
    where event.id = %s and attempt.id = %s and attempt.ended is null and attempt.request = event.request returning event.id
    """
    END: Final = """
    with ended as (update attempt set ended_at = now(), ended = %s where id = %s and ended is null returning request)
    update request set stands = %s, ended_at = now(), answer = %s, error = %s,
        release_verdict = case when work = 'moving' then %s end
    from ended where request.id = ended.request
    returning request.id
    """
    GIVE_UP: Final = """
    update request set stands = 'failed', ended_at = now(), error = concat_ws(' ', error, %s::text) where id = %s returning *
    """
    LOCK_WRITERS: Final = "lock table request in share row exclusive mode"
    READ: Final = "select * from request where id = %s"
    READ_TO_TAKE_UP: Final = "select * from request where id = %s for update"


def key_reused(written: Request) -> Refused:
    return Refused(
        f"The Idempotency-Key was given with request {written.id}, which asked something else: "
        f"{written.attempted()}. Nothing was written. Ask with a new key, or with none."
    )


def said(asked: Asked) -> list[JsonValue]:
    return [asked.product, asked.lane, asked.work.value, asked.build, asked.delete_data_set]


# The request a key is answered with is the one it is always answered with, however that request has ended since.
def keep_key(session: Session, gate: Gate, idempotency_key: str | None, request: Request) -> Request:
    if idempotency_key is not None:
        session.execute(Sql.KEEP_KEY, gate.made(key_digest(idempotency_key), request.id))
    return request


# A key says only that a call is one sent before. With a key not seen, a call is held to its lane's rule like any other.
def same_as_written(session: Session, gate: Gate, asked: Asked, idempotency_key: str | None) -> Request | None:
    row = session.execute(Sql.SAME_KEY, gate.made(key_digest(idempotency_key))).fetchone()
    if row is not None:
        same_key = Request.model_validate(row)
        if same_key.asked() != asked:
            raise key_reused(same_key)
        return same_key
    row = session.execute(Sql.SAME_UNFINISHED, gate.sent(*said(asked))).fetchone()
    return None if row is None else keep_key(session, gate, idempotency_key, Request.model_validate(row))


def write_new(session: Session, gate: Gate, asked: Asked, asked_by: str, idempotency_key: str | None) -> Request:
    passed = gate.sent(*said(asked), asked_by, new_generation())
    waited = [row["id"] for row in session.execute(Sql.END_WAITING, gate.sent(asked.product, asked.lane)).fetchall()]
    new = Request.model_validate(session.execute(Sql.WRITE, passed).fetchone())
    session.execute(Sql.REPLACED_BY, gate.made(new.id, list[JsonValue](waited)))
    return keep_key(session, gate, idempotency_key, new)


def unreachable(doing: str, error: psycopg.Error) -> RecordError:
    return RecordError(f"Launchplane's record could not be reached while {doing}: {str(error).strip().rstrip('.')}.")


def asked_once(session: Session, doing: str, query: LiteralString, passed: Passed | None) -> DictRow:
    try:
        row = session.execute(query, passed).fetchone()
    except psycopg.Error as error:
        raise unreachable(doing, error) from error
    if row is None:
        raise RecordError(f"Launchplane's record gave no answer while {doing}.")
    return row


# The caller has made the transaction one reading, so no attempt or event is seen that began after the requests were read.
def with_history(session: Session, gate: Gate, rows: Sequence[DictRow]) -> list[Kept]:
    ids = gate.made([row["id"] for row in rows])
    attempts = session.execute(Sql.ATTEMPTS, ids).fetchall()
    events = session.execute(Sql.EVENTS, ids).fetchall()
    numbers: dict[int, int] = {}
    of: dict[int, list[Attempt]] = {row["id"]: [] for row in rows}
    for attempt in attempts:
        numbers[attempt["id"]] = len(of[attempt["request"]]) + 1
        of[attempt["request"]].append(Attempt.model_validate({**attempt, "number": numbers[attempt["id"]]}))
    return [
        Kept.model_validate(
            {
                **row,
                "attempts": of[row["id"]],
                "events": [{**event, "attempt": numbers[event["attempt"]]} for event in events if event["request"] == row["id"]],
            }
        )
        for row in rows
    ]


class PostgresRecord(Record):
    def __init__(self, database_named: str, secrets: Secrets, seconds_to_reach: float = SECONDS_TO_REACH_THE_DATABASE) -> None:
        self.database = database_named
        self.gate = Gate(secrets)
        # As long again as reaching it may take, so a database that cannot be reached is said to be that.
        self.seconds_to_answer = 2 * seconds_to_reach
        # Each connection is checked as it is taken, so one the database dropped is replaced and not handed out.
        self.pool = ConnectionPool(
            database_named,
            min_size=1,
            max_size=MOST_CONNECTIONS,
            timeout=seconds_to_reach,
            check=ConnectionPool.check_connection,
            open=False,
        )
        self.pool.open()
        # Fewer than the pool has connections, so readings the record never answers leave connections for the workers.
        self.readers = ThreadPoolExecutor(max_workers=MOST_READINGS_AT_ONCE, thread_name_prefix="record-reading")

    def close(self) -> None:
        self.readers.shutdown(wait=False, cancel_futures=True)
        self.pool.close()

    # One transaction on a connection of the pool. It is never held while Launchplane talks to the container host.
    @contextmanager
    def session(self, doing: str) -> Generator[Session]:
        try:
            with self.pool.connection() as connection, connection.cursor(row_factory=dict_row) as cursor:
                yield Session(cursor)
        except psycopg.Error as error:
            raise unreachable(doing, error) from error

    # One reading of the record in one transaction, given up on when the record has not answered in time. The reading
    # itself is left to end when the record answers.
    def reading[Answer](self, doing: str, read: Callable[[Session], Answer]) -> Answer:
        def read_at_one_moment() -> Answer:
            with self.session(doing) as session:
                session.execute(Sql.ONE_READING)
                return read(session)

        waited_for = self.readers.submit(read_at_one_moment)
        try:
            return waited_for.result(self.seconds_to_answer)
        except TimeoutError:
            waited_for.cancel()
            raise RecordError(f"Launchplane's record did not answer within {self.seconds_to_answer:g} s while {doing}.") from None

    def write(self, asked: Asked, asked_by: str, idempotency_key: str | None) -> Request:
        # A record that fails may hold the request all the same: the call may repeat one it holds, or the write may have landed.
        try:
            with self.session("writing a request") as session:
                # Two calls asking the same thing at once write one request: the second waits here and finds the first.
                session.execute(Sql.LOCK_WRITERS)
                same = same_as_written(session, self.gate, asked, idempotency_key)
                return same or write_new(session, self.gate, asked, asked_by, idempotency_key)
        except RecordError as error:
            raise Unconfirmed(str(error)) from error

    def read(self, request: int) -> Kept | None:
        def one(session: Session) -> Kept | None:
            rows = session.execute(Sql.READ, self.gate.made(request)).fetchall()
            return with_history(session, self.gate, rows)[0] if rows else None

        return self.reading("reading a request", one)

    def unfinished(self) -> list[Kept]:
        return self.reading(
            "reading the requests not finished",
            lambda session: with_history(session, self.gate, session.execute(Sql.UNFINISHED).fetchall()),
        )

    def recently_ended(self) -> list[Request]:
        return self.reading(
            "reading the requests that ended",
            lambda session: [Request.model_validate(row) for row in session.execute(Sql.RECENTLY_ENDED).fetchall()],
        )

    # Every request that ended a short while ago and was replaced or had a cancel asked of it, however it ended.
    def cancels_answered(self) -> list[Request]:
        shown = self.gate.made(MINUTES_AN_ANSWERED_CANCEL_IS_SHOWN)
        return self.reading(
            "reading the requests that were replaced or asked to cancel",
            lambda session: [Request.model_validate(row) for row in session.execute(Sql.CANCELS_ANSWERED, shown).fetchall()],
        )

    # The oldest request of each lane that takes one, oldest first.
    def next_up(self) -> list[Request]:
        with self.session("looking for the next requests") as session:
            return [Request.model_validate(row) for row in session.execute(Sql.NEXT_UP).fetchall()]

    def turn(self, product: str, lane: str) -> AbstractContextManager[Held | None]:
        return self.turn_held(product, lane)

    # A lane's turn is a lock held by a connection of its own, so it is let go when the work ends or this Launchplane dies,
    # and never apart from the work. What is given asks that connection whether it still holds the lock.
    @contextmanager
    def turn_held(self, product: str, lane: str) -> Generator[Held | None]:
        try:
            connection = psycopg.connect(self.database, autocommit=True)
        except psycopg.Error as error:
            raise unreachable("taking a lane's turn", error) from error
        with closing(connection), connection.cursor(row_factory=dict_row) as cursor:
            session = Session(cursor)

            def held() -> bool:
                try:
                    return asked_once(session, "asking whether a lane's turn is held", Sql.TURN_HELD, None)["held"] > 0
                except RecordError:
                    return False

            ours = asked_once(session, "taking a lane's turn", Sql.TAKE_TURN, self.gate.made(f"{product}/{lane}"))["ours"]
            yield held if ours else None

    # A request found working with nobody holding its lane was interrupted, and is taken up again however often that was.
    def take_up(self, request: int) -> Taken | None:
        with self.session("taking up a request") as session:
            taken = self.gate.made(request)
            row = session.execute(Sql.READ_TO_TAKE_UP, taken).fetchone()
            found = None if row is None else Request.model_validate(row)
            if found is None or found.stands not in (Stands.WAITING, Stands.WORKING):
                return None
            session.execute(Sql.INTERRUPT, taken)
            attempt = asked_once(session, "beginning an attempt", Sql.BEGIN_ATTEMPT, self.gate.made(request, request))["id"]
            return Taken(attempt, again=found.stands is Stands.WORKING)

    # The attempt is over and the request is not: it stays working, with why it is not finished, and is taken up again.
    def leave_open(self, attempt: int, why: str) -> None:
        with self.session("writing why a request is not finished") as session:
            if session.execute(Sql.LEAVE_OPEN, self.gate.made(attempt, why)).fetchone() is None:
                raise Interrupted(f"Attempt {attempt} has ended in the record, so why it is not finished was not written.")

    def note(self, attempt: int, did: str, error: str | None) -> None:
        with self.session("writing what was done") as session:
            if session.execute(Sql.NOTE, self.gate.made(did, error, attempt)).fetchone() is None:
                raise Interrupted(f"Attempt {attempt} has ended in the record, so what it did next was not written: {did}")

    def about_to(self, attempt: int, did: str, action: dict[str, JsonValue]) -> int:
        with self.session("writing what is about to be done") as session:
            line = Sent.model_validate(action).model_dump(mode="json")
            row = session.execute(Sql.ABOUT_TO, self.gate.made(did, line, attempt)).fetchone()
            if row is None:
                raise Interrupted(f"Attempt {attempt} has ended in the record, so it was not written and not done: {did}")
            return row["id"]

    def complete(self, attempt: int, line: int, outcome: dict[str, JsonValue], error: str | None = None) -> None:
        with self.session("writing what came of a change") as session:
            known = Reply.model_validate(outcome).model_dump(mode="json", exclude_unset=True)
            if session.execute(Sql.COMPLETE, self.gate.made(known, error, line, attempt)).fetchone() is None:
                raise Interrupted(f"Attempt {attempt} has ended in the record, so what came of a change was not written.")

    # A move with a cancel on record ends only on what the gate decided of that cancel: abandoned, or committed. With
    # neither, nothing is written, and the worker carries the cancel to the gate and plans again.
    def end(
        self, attempt: int, stands: Stands, answer: dict[str, Any] | None, error: str | None, *, decided: Verdict | None = None
    ) -> bool:
        with self.session("writing how a request ended") as session:
            open_attempt = session.execute(Sql.OPEN_ATTEMPT, self.gate.made(attempt)).fetchone()
            if open_attempt is None:
                raise Interrupted(f"Attempt {attempt} has ended in the record, so its answer was not written.")
            if open_attempt["move_with_a_cancel"] and decided not in (Verdict.ABANDONED, Verdict.COMMITTED):
                return False
            verdict = None if decided is None else decided.value
            session.execute(Sql.END, self.gate.made(stands.value, attempt, stands.value, answer, error, verdict))
            return True

    # Only a request at work with no attempt open and none after `last`, so nothing was done for it since `last` was
    # read, and with no cancel asked: that one is the cancel's to end. Its row is locked until it is ended.
    def give_up(self, request: int, last: int) -> Request | None:
        with self.session("giving a request up") as session:
            given_up = self.gate.made(request)
            row = session.execute(Sql.READ_TO_TAKE_UP, given_up).fetchone()
            attempts = asked_once(session, "reading the attempts of a request", Sql.RESTING, given_up)
            resting = row is not None and (row["stands"], row["cancel_asked_at"]) == (Stands.WORKING, None)
            if not resting or (attempts["open"], attempts["last"]) != (0, last):
                return None
            return Request.model_validate(session.execute(Sql.GIVE_UP, self.gate.made(GIVEN_UP, request)).fetchone())

    def cancel(self, request: int, by: str) -> Request | None:
        with self.session("cancelling a request") as session:
            # A request that waits ends with nothing done, and one being worked is marked for its worker to read.
            session.execute(Sql.CANCEL, self.gate.sent(PutBack().model_dump(), by, request))
            row = session.execute(Sql.READ, self.gate.made(request)).fetchone()
            return None if row is None else Request.model_validate(row)

    def cancel_asked(self, request: int) -> bool:
        with self.session("reading whether a cancel was asked of a request") as session:
            row = session.execute(Sql.CANCEL_ASKED, self.gate.made(request)).fetchone()
            return row is not None and row["asked"]

    # Written by whoever learns it at the gate, the call that cancels or the worker. The first reason stays.
    def refuse_cancel(self, request: int, why: str) -> None:
        with self.session("writing why a request was not cancelled") as session:
            session.execute(Sql.REFUSE_CANCEL, self.gate.made(why, request))
