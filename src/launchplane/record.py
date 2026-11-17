import hashlib
import os
from collections.abc import Callable, Generator, Sequence
from contextlib import AbstractContextManager, closing, contextmanager
from datetime import datetime
from enum import StrEnum
from typing import Any, Final, LiteralString, Protocol

import psycopg
from psycopg.rows import DictRow, dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, ConfigDict

from launchplane.judge import Work

MOST_ATTEMPTS = 3
DATABASE = "postgresql:///launchplane?host=/var/run/postgresql"
SECONDS_TO_REACH_THE_DATABASE = 3.0

type Session = psycopg.Cursor[DictRow]
# Asked before each action on the host: whether the lane's turn is still held.
type Held = Callable[[], bool]


class RecordError(Exception):
    pass


# Writing a request may or may not have reached the record, so the request may be worked.
class Unconfirmed(RecordError):
    pass


class Refused(Exception):
    pass


# Another Launchplane has taken the request up since this attempt began, so this attempt writes nothing more.
class Interrupted(Exception):
    pass


class Stands(StrEnum):
    WAITING = "waiting"
    WORKING = "working"
    DONE = "done"
    FAILED = "failed"
    NEEDS_A_PERSON = "needs a person"


ENDED = (Stands.DONE, Stands.FAILED)

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
    asked_by: str
    asked_at: datetime
    stands: Stands
    ended_at: datetime | None
    answer: dict[str, Any] | None
    error: str | None

    def asked(self) -> Asked:
        return Asked.model_validate(self.model_dump(include=set(Asked.model_fields)))


class Attempt(BaseModel):
    number: int
    began_at: datetime
    ended_at: datetime | None
    ended: str | None


class Event(BaseModel):
    at: datetime
    attempt: int
    did: str
    error: str | None


class Kept(Request):
    attempts: list[Attempt]
    events: list[Event]


class Record(Protocol):
    def write(self, asked: Asked, asked_by: str, idempotency_key: str | None) -> Request: ...
    def read(self, request: int) -> Kept | None: ...
    def unfinished(self) -> list[Kept]: ...
    def recently_ended(self) -> list[Request]: ...
    def next_up(self) -> Request | None: ...
    def turn(self, product: str, lane: str) -> AbstractContextManager[Held | None]: ...
    def take_up(self, request: int) -> int | None: ...
    def note(self, attempt: int, did: str, error: str | None) -> None: ...
    def end(self, attempt: int, stands: Stands, answer: dict[str, Any] | None, error: str | None) -> None: ...
    def give_up(self, request: int) -> Request | None: ...
    def retry(self, request: int, asked_by: str) -> Request | None: ...


def database() -> str:
    return os.environ.get("LAUNCHPLANE_DATABASE", DATABASE)


# A caller never needs its key shown back, so only this is kept: a key that is a secret cannot be read from the record.
def key_digest(idempotency_key: str | None) -> str | None:
    return None if idempotency_key is None else hashlib.sha256(idempotency_key.encode()).hexdigest()


def needs_a_person(request: Request) -> str:
    return (
        f"{request.attempted()} was taken up {MOST_ATTEMPTS} times and never ended: each time Launchplane stopped, or lost its "
        f"record, while working on it. Launchplane does not take it up again by itself. Look at the lane, then ask for it again "
        f"with POST /requests/{request.id}/retry or give it up with POST /requests/{request.id}/give-up. Until then "
        f"{request.lane} of {request.product} takes no other request."
    )


GIVEN_UP = " A person gave it up."
ASKED_AGAIN = " A person asked for it again, as request {again}."


# PyCharm has no schema to resolve these against. The tests run every statement against PostgreSQL.
# noinspection SqlResolve
class Sql:
    SAME_KEY: Final = "select * from request where key_digest = %s"
    WAITS_ON_A_PERSON: Final = """
    select id from request where (product, lane) = (%s, %s) and stands = 'needs a person' order by id limit 1
    """
    SAME_UNFINISHED: Final = """
    select * from request
    where (product, lane, work) = (%s, %s, %s) and build is not distinct from %s and delete_data_set is not distinct from %s
        and stands in ('waiting', 'working')
    order by id limit 1
    """
    WRITE: Final = """
    insert into request (product, lane, work, build, delete_data_set, asked_by, key_digest)
    values (%s, %s, %s, %s, %s, %s, %s) returning *
    """
    UNFINISHED: Final = "select * from request where stands in ('waiting', 'working', 'needs a person') order by id"
    RECENTLY_ENDED: Final = "select * from request where stands in ('done', 'failed') order by ended_at desc, id desc limit 20"
    ATTEMPTS: Final = "select id, request, began_at, ended_at, ended from attempt where request = any(%s) order by id"
    EVENTS: Final = "select request, attempt, at, did, error from event where request = any(%s) order by id"
    NEXT_UP: Final = """
    select * from request asked
    where stands in ('waiting', 'working') and not exists (
        select from request stuck where (stuck.product, stuck.lane) = (asked.product, asked.lane) and stuck.stands = 'needs a person'
    )
    order by id limit 1
    """
    ONE_READING: Final = "set transaction isolation level repeatable read, read only"
    TURN_HELD: Final = "select count(*) as held from pg_locks where locktype = 'advisory' and pid = pg_backend_pid() and granted"
    TAKE_TURN: Final = "select pg_try_advisory_lock(hashtextextended(%s, 0)) as ours"
    INTERRUPT: Final = "update attempt set ended_at = now(), ended = 'interrupted' where request = %s and ended is null"
    COUNT_ATTEMPTS: Final = "select count(*) from attempt where request = %s"
    STOP_FOR_A_PERSON: Final = "update request set stands = 'needs a person', error = %s where id = %s"
    BEGIN_ATTEMPT: Final = """
    with taken as (update request set stands = 'working' where id = %s)
    insert into attempt (request) values (%s) returning id
    """
    NOTE: Final = """
    insert into event (request, attempt, did, error)
    select request, id, %s, %s from attempt where id = %s and ended is null returning id
    """
    END: Final = """
    with ended as (update attempt set ended_at = now(), ended = %s where id = %s and ended is null returning request)
    update request set stands = %s, ended_at = now(), answer = %s, error = %s from ended where request.id = ended.request
    returning request.id
    """
    GIVE_UP: Final = """
    update request set stands = 'failed', ended_at = now(), error = error || %s
    where id = %s and stands = 'needs a person' returning *
    """
    STUCK: Final = "select * from request where id = %s and stands = 'needs a person' for update"
    LOCK_WRITERS: Final = "lock table request in share row exclusive mode"
    READ: Final = "select * from request where id = %s"
    READ_TO_TAKE_UP: Final = "select * from request where id = %s for update"


def key_reused(written: Request) -> Refused:
    return Refused(
        f"The Idempotency-Key was given with request {written.id}, which asked something else: "
        f"{written.attempted()}. Nothing was written. Ask with a new key, or with none."
    )


def lane_waits_on_a_person(asked: Asked, stuck: int) -> Refused:
    return Refused(
        f"{asked.lane} of {asked.product} takes no request while request {stuck} needs a person. Nothing was written. "
        f"Read it with GET /requests/{stuck}, then ask for it again with POST /requests/{stuck}/retry or give "
        f"it up with POST /requests/{stuck}/give-up."
    )


def same_as_written(session: Session, asked: Asked, idempotency_key: str | None) -> Request | None:
    if idempotency_key is None:
        values = [asked.product, asked.lane, asked.work, asked.build, asked.delete_data_set]
        row = session.execute(Sql.SAME_UNFINISHED, values).fetchone()
        return None if row is None else Request.model_validate(row)
    row = session.execute(Sql.SAME_KEY, [key_digest(idempotency_key)]).fetchone()
    if row is None:
        return None
    same_key = Request.model_validate(row)
    if same_key.asked() != asked:
        raise key_reused(same_key)
    return same_key


def refuse_while_a_person_is_awaited(session: Session, asked: Asked) -> None:
    stuck = session.execute(Sql.WAITS_ON_A_PERSON, [asked.product, asked.lane]).fetchone()
    if stuck is not None:
        raise lane_waits_on_a_person(asked, stuck["id"])


def write_new(session: Session, asked: Asked, asked_by: str, idempotency_key: str | None) -> Request:
    values = [asked.product, asked.lane, asked.work, asked.build, asked.delete_data_set, asked_by, key_digest(idempotency_key)]
    return Request.model_validate(session.execute(Sql.WRITE, values).fetchone())


def unreachable(doing: str, error: psycopg.Error) -> RecordError:
    return RecordError(f"Launchplane's record could not be reached while {doing}: {str(error).strip().rstrip('.')}.")


def asked_once(session: Session, doing: str, query: LiteralString, values: list[Any]) -> DictRow:
    try:
        row = session.execute(query, values).fetchone()
    except psycopg.Error as error:
        raise unreachable(doing, error) from error
    if row is None:
        raise RecordError(f"Launchplane's record gave no answer while {doing}.")
    return row


# The caller has made the transaction one reading, so no attempt or event is seen that began after the requests were read.
def with_history(session: Session, rows: Sequence[DictRow]) -> list[Kept]:
    ids = [row["id"] for row in rows]
    attempts = session.execute(Sql.ATTEMPTS, [ids]).fetchall()
    events = session.execute(Sql.EVENTS, [ids]).fetchall()
    numbers: dict[int, int] = {}
    of: dict[int, list[Attempt]] = {each: [] for each in ids}
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
    def __init__(self, database_named: str, seconds_to_reach: float = SECONDS_TO_REACH_THE_DATABASE) -> None:
        self.database = database_named
        # Each connection is checked as it is taken, so one the database dropped is replaced and not handed out.
        self.pool = ConnectionPool(
            database_named, min_size=1, max_size=4, timeout=seconds_to_reach, check=ConnectionPool.check_connection, open=False
        )
        self.pool.open()

    def close(self) -> None:
        self.pool.close()

    # One transaction on a connection of the pool. It is never held while Launchplane talks to the container host.
    @contextmanager
    def session(self, doing: str) -> Generator[Session]:
        try:
            with self.pool.connection() as connection, connection.cursor(row_factory=dict_row) as session:
                yield session
        except psycopg.Error as error:
            raise unreachable(doing, error) from error

    def write(self, asked: Asked, asked_by: str, idempotency_key: str | None) -> Request:
        sent = False
        try:
            with self.session("writing a request") as session:
                # Two calls asking the same thing at once write one request: the second waits here and finds the first.
                session.execute(Sql.LOCK_WRITERS)
                same = same_as_written(session, asked, idempotency_key)
                if same is not None:
                    return same
                refuse_while_a_person_is_awaited(session, asked)
                sent = True
                return write_new(session, asked, asked_by, idempotency_key)
        except RecordError as error:
            if sent:
                raise Unconfirmed(str(error)) from error
            raise

    def read(self, request: int) -> Kept | None:
        with self.session("reading a request") as session:
            session.execute(Sql.ONE_READING)
            rows = session.execute(Sql.READ, [request]).fetchall()
            return with_history(session, rows)[0] if rows else None

    def unfinished(self) -> list[Kept]:
        with self.session("reading the requests not finished") as session:
            session.execute(Sql.ONE_READING)
            return with_history(session, session.execute(Sql.UNFINISHED).fetchall())

    def recently_ended(self) -> list[Request]:
        with self.session("reading the requests that ended") as session:
            return [Request.model_validate(row) for row in session.execute(Sql.RECENTLY_ENDED).fetchall()]

    def next_up(self) -> Request | None:
        with self.session("looking for the next request") as session:
            row = session.execute(Sql.NEXT_UP).fetchone()
            return None if row is None else Request.model_validate(row)

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
        with closing(connection), connection.cursor(row_factory=dict_row) as session:

            def held() -> bool:
                try:
                    return asked_once(session, "asking whether a lane's turn is held", Sql.TURN_HELD, [])["held"] > 0
                except RecordError:
                    return False

            ours = asked_once(session, "taking a lane's turn", Sql.TAKE_TURN, [f"{product}/{lane}"])["ours"]
            yield held if ours else None

    # A request found working with nobody holding its lane was interrupted. That is counted, never taken for waiting.
    def take_up(self, request: int) -> int | None:
        with self.session("taking up a request") as session:
            row = session.execute(Sql.READ_TO_TAKE_UP, [request]).fetchone()
            found = None if row is None else Request.model_validate(row)
            if found is None or found.stands not in (Stands.WAITING, Stands.WORKING):
                return None
            session.execute(Sql.INTERRUPT, [request])
            if asked_once(session, "counting attempts", Sql.COUNT_ATTEMPTS, [request])["count"] >= MOST_ATTEMPTS:
                session.execute(Sql.STOP_FOR_A_PERSON, [needs_a_person(found), request])
                return None
            return asked_once(session, "beginning an attempt", Sql.BEGIN_ATTEMPT, [request, request])["id"]

    def note(self, attempt: int, did: str, error: str | None) -> None:
        with self.session("writing what was done") as session:
            if session.execute(Sql.NOTE, [did, error, attempt]).fetchone() is None:
                raise Interrupted(f"Attempt {attempt} has ended in the record, so what it did next was not written: {did}")

    def end(self, attempt: int, stands: Stands, answer: dict[str, Any] | None, error: str | None) -> None:
        with self.session("writing how a request ended") as session:
            values = [stands, attempt, stands, None if answer is None else Jsonb(answer), error]
            if session.execute(Sql.END, values).fetchone() is None:
                raise Interrupted(f"Attempt {attempt} has ended in the record, so its answer was not written.")

    def give_up(self, request: int) -> Request | None:
        with self.session("giving a request up") as session:
            row = session.execute(Sql.GIVE_UP, [GIVEN_UP, request]).fetchone()
            return None if row is None else Request.model_validate(row)

    def retry(self, request: int, asked_by: str) -> Request | None:
        with self.session("asking for a request again") as session:
            row = session.execute(Sql.STUCK, [request]).fetchone()
            if row is None:
                return None
            again = write_new(session, Request.model_validate(row).asked(), asked_by, None)
            session.execute(Sql.GIVE_UP, [ASKED_AGAIN.format(again=again.id), request])
            return again
