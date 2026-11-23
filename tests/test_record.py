import threading
import time
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractContextManager
from typing import Any, LiteralString, get_args, get_origin, get_type_hints

import psycopg
import pytest
from alembic import command
from alembic.config import Config

from launchplane import record as record_module
from launchplane.instance import Verdict
from launchplane.judge import Work
from launchplane.page import lines_of, request_note
from launchplane.record import (
    Asked,
    CarriesASecret,
    Interrupted,
    PostgresRecord,
    Record,
    RecordError,
    Refused,
    Reply,
    Sent,
    Stands,
    Unconfirmed,
)
from tests.conftest import ALEMBIC
from tests.given_secrets import NO_SECRETS, GivenSecrets
from tests.pretend_record import PretendRecord, begun, lose_every_turn

MOVE_B = Asked(product="site", lane="testing", work=Work.MOVE, build="site:b")
MOVE_C = Asked(product="site", lane="testing", work=Work.MOVE, build="site:c")
RETIRE = Asked(product="site", lane="testing", work=Work.RETIRE)
RETIRE_OTHER = Asked(product="site", lane="other", work=Work.RETIRE)
CALLER = "the address 192.0.2.1"


# Taken up and left open: at work, with no attempt of it open. The last attempt it was given.
def resting(record: Record, request: int) -> int:
    attempt = begun(record, request)
    record.leave_open(attempt, "The host could not be read.")
    return attempt


def test_asking_the_same_of_a_lane_while_one_is_unfinished_writes_no_second_request(record: Record) -> None:
    first = record.write(MOVE_B, CALLER, None)
    again = record.write(MOVE_B, "the address 192.0.2.2", None)
    other = record.write(MOVE_C, CALLER, None)
    assert (again.id, again.asked_by, other.id != first.id) == (first.id, CALLER, True)


def at_work_on_b(record: Record) -> int:
    request = record.write(MOVE_B, CALLER, None)
    assert record.take_up(request.id) is not None
    return request.id


def test_a_request_for_a_lane_with_nothing_unfinished_waits_and_is_next_up_at_once(record: Record) -> None:
    request = record.write(MOVE_B, CALLER, None)
    assert (request.stands, [each.id for each in record.next_up()]) == (Stands.WAITING, [request.id])


def test_a_request_arriving_while_one_is_at_work_waits_and_the_one_at_work_is_untouched(record: Record) -> None:
    at_work = at_work_on_b(record)
    before = record.read(at_work)
    arrived = record.write(MOVE_C, CALLER, None)
    assert (arrived.stands, record.read(at_work), [each.id for each in record.next_up()]) == (Stands.WAITING, before, [at_work])


def test_a_request_arriving_while_one_is_at_work_and_one_waits_replaces_the_one_waiting_which_says_by_which(
    record: Record,
) -> None:
    at_work = at_work_on_b(record)
    before = record.read(at_work)
    waited, newest = record.write(MOVE_C, CALLER, None), record.write(RETIRE, CALLER, None)
    replaced = record.read(waited.id)
    assert replaced is not None
    assert (replaced.stands, replaced.replaced_by, replaced.attempts, replaced.cancel_asked_by, replaced.ended_at is None) == (
        Stands.REPLACED,
        newest.id,
        [],
        None,
        False,
    )
    assert (record.read(at_work), [each.id for each in record.unfinished()]) == (before, [at_work, newest.id])
    assert record.take_up(waited.id) is None


def test_asking_exactly_what_an_unfinished_request_asks_is_answered_with_that_request_and_replaces_nothing(
    record: Record,
) -> None:
    at_work = at_work_on_b(record)
    waiting = record.write(MOVE_C, CALLER, None)
    again = [record.write(MOVE_B, CALLER, None).id, record.write(MOVE_C, CALLER, None).id]
    assert (again, [(each.id, each.stands) for each in record.unfinished()]) == (
        [at_work, waiting.id],
        [(at_work, Stands.WORKING), (waiting.id, Stands.WAITING)],
    )


def test_a_key_not_seen_before_exempts_no_request_from_its_lanes_rule_and_a_lane_never_has_two_waiting(record: Record) -> None:
    at_work = at_work_on_b(record)
    waiting = record.write(MOVE_C, CALLER, "first")
    same = [record.write(MOVE_C, CALLER, "second").id, record.write(MOVE_B, CALLER, "third").id]
    newest = record.write(RETIRE, CALLER, "fourth")
    assert (same, [(each.id, each.stands) for each in record.unfinished()], record.write(MOVE_C, CALLER, "first").stands) == (
        [waiting.id, at_work],
        [(at_work, Stands.WORKING), (newest.id, Stands.WAITING)],
        Stands.REPLACED,
    )


def test_the_record_itself_refuses_a_lane_a_second_request_that_waits(
    postgres_record: PostgresRecord, migrated_database: str
) -> None:
    waiting = postgres_record.write(MOVE_B, CALLER, None)
    # noinspection SqlResolve
    second = "insert into request (product, lane, work, asked_by, generation) values ('site', 'testing', 'retiring', 'a test', 'g')"
    with psycopg.connect(migrated_database) as by_hand, pytest.raises(psycopg.errors.UniqueViolation):
        by_hand.execute(second)
    assert [each.id for each in postgres_record.unfinished()] == [waiting.id]


# Held at the moment its row is locked to be taken up, as a worker holds it, while a newer request arrives. PostgreSQL
# ends one of the two, and whichever it is, a request is never both replaced and worked.
# noinspection SqlResolve
def test_a_request_being_taken_up_as_a_newer_one_arrives_is_worked_or_replaced_and_never_both(
    postgres_record: PostgresRecord, migrated_database: str
) -> None:
    waited = postgres_record.write(MOVE_B, CALLER, None)
    with psycopg.connect(migrated_database) as taking_up, ThreadPoolExecutor(max_workers=1) as pool:
        taking_up.execute("select * from request where id = %s for update", [waited.id])
        arriving = pool.submit(postgres_record.write, MOVE_C, CALLER, None)
        time.sleep(0.3)
        try:
            taking_up.execute("update request set stands = 'working' where id = %s and stands = 'waiting'", [waited.id])
            taking_up.commit()
        except psycopg.errors.DeadlockDetected:
            taking_up.rollback()
        arrived = arriving.exception() is None
    kept = postgres_record.read(waited.id)
    assert kept is not None
    assert (kept.stands, kept.replaced_by is None, arrived) in ((Stands.WORKING, True, False), (Stands.REPLACED, False, True))


def test_asking_the_same_again_once_the_first_has_ended_writes_a_new_request(record: Record) -> None:
    first = record.write(MOVE_B, CALLER, None)
    attempt = begun(record, first.id)
    record.end(attempt, Stands.DONE, {"outcome": "switched"}, None)
    assert record.write(MOVE_B, CALLER, None).id != first.id


def test_the_same_key_and_body_answers_with_the_request_already_written_even_after_it_ended(record: Record) -> None:
    first = record.write(MOVE_B, CALLER, "key-1")
    attempt = begun(record, first.id)
    record.end(attempt, Stands.DONE, {"outcome": "switched"}, None)
    again = record.write(MOVE_B, CALLER, "key-1")
    assert (again.id, again.stands, again.answer) == (first.id, Stands.DONE, {"outcome": "switched"})


def test_the_same_key_with_another_body_is_refused_and_writes_nothing(record: Record) -> None:
    first = record.write(MOVE_B, CALLER, "key-1")
    with pytest.raises(Refused, match=f"request {first.id}"):
        record.write(MOVE_C, CALLER, "key-1")
    assert [each.id for each in record.unfinished()] == [first.id]


def test_two_calls_at_once_asking_the_same_write_one_request(record: Record) -> None:
    def write(_caller: int) -> int:
        return record.write(MOVE_B, CALLER, None).id

    with ThreadPoolExecutor(max_workers=8) as pool:
        written = list(pool.map(write, range(8)))
    assert len(set(written)) == 1


def test_the_oldest_request_of_each_lane_is_next_up_and_a_later_one_of_a_lane_only_once_the_earlier_has_ended(
    record: Record,
) -> None:
    first, other = record.write(MOVE_B, CALLER, None), record.write(RETIRE_OTHER, CALLER, None)
    attempt = begun(record, first.id)
    later = record.write(MOVE_C, CALLER, None)
    while_the_first_is_worked = [each.id for each in record.next_up()]
    record.end(attempt, Stands.FAILED, None, "stopped", decided=Verdict.ABANDONED)
    assert (while_the_first_is_worked, [each.id for each in record.next_up()]) == ([first.id, other.id], [other.id, later.id])


def test_a_request_interrupted_five_times_is_taken_up_a_sixth_time_and_a_newer_request_waits_behind_it(record: Record) -> None:
    request = record.write(MOVE_B, CALLER, None)
    taken = [record.take_up(request.id) for _ in range(6)]
    behind = record.write(MOVE_C, CALLER, None)
    kept = record.read(request.id)
    assert kept is not None and None not in taken
    assert [each.again for each in taken if each is not None] == [False, True, True, True, True, True]
    assert (kept.stands, [(each.number, each.ended) for each in kept.attempts], behind.stands) == (
        Stands.WORKING,
        [*((number, "interrupted") for number in range(1, 6)), (6, None)],
        Stands.WAITING,
    )
    assert [each.id for each in record.next_up()] == [request.id]


def test_an_attempt_that_was_interrupted_writes_nothing_more(record: Record) -> None:
    request = record.write(MOVE_B, CALLER, None)
    first, second = begun(record, request.id), begun(record, request.id)
    with pytest.raises(Interrupted):
        record.note(first, "start the instance site-testing-1", None)
    with pytest.raises(Interrupted):
        record.end(first, Stands.DONE, {"outcome": "switched"}, None)
    record.note(second, "check site-testing-1 at /", "Connection refused")
    kept = record.read(request.id)
    assert kept is not None
    assert (kept.stands, [(each.attempt, each.did, each.error) for each in kept.events]) == (
        Stands.WORKING,
        [(2, "check site-testing-1 at /", "Connection refused")],
    )


def test_giving_up_a_request_at_work_with_no_attempt_open_ends_it_as_failed_and_its_lane_works_the_next(record: Record) -> None:
    request = record.write(MOVE_B, CALLER, None)
    last = resting(record, request.id)
    behind = record.write(MOVE_C, CALLER, None)
    given_up = record.give_up(request.id, last)
    assert given_up is not None and given_up.error is not None
    assert (given_up.stands, given_up.error.startswith("The host could not be read. A person gave it up"), given_up.answer) == (
        Stands.FAILED,
        True,
        None,
    )
    assert [each.id for each in record.next_up()] == [behind.id]


def test_a_request_being_worked_or_taken_up_since_its_lane_was_read_or_waiting_is_not_given_up(record: Record) -> None:
    waiting = record.write(RETIRE_OTHER, CALLER, None)
    request = record.write(MOVE_B, CALLER, None)
    read_before = resting(record, request.id)
    being_worked = begun(record, request.id)
    while_an_attempt_is_open = record.give_up(request.id, being_worked)
    record.leave_open(being_worked, "The host could not be read.")
    taken_up_since = record.give_up(request.id, read_before)
    refused = [while_an_attempt_is_open, taken_up_since, record.give_up(waiting.id, 0), record.give_up(999, 0)]
    kept, still_waiting = record.read(request.id), record.read(waiting.id)
    assert kept is not None and still_waiting is not None
    assert (refused, kept.stands, still_waiting.stands) == ([None] * 4, Stands.WORKING, Stands.WAITING)


def test_a_lanes_turn_has_one_holder_and_is_free_again_when_the_holder_lets_go(record: Record) -> None:
    with record.turn("site", "testing") as first, record.turn("site", "testing") as second, record.turn("site", "other") as other:
        assert first is not None and other is not None
        assert (first(), second, other()) == (True, None, True)
    with record.turn("site", "testing") as after:
        assert after is not None


def test_a_lanes_turn_is_let_go_when_its_holder_dies_without_letting_go(postgres_record: PostgresRecord) -> None:
    holder = psycopg.connect(postgres_record.database, autocommit=True)
    holder.execute("select pg_advisory_lock(hashtextextended('site/testing', 0))")
    with postgres_record.turn("site", "testing") as while_held:
        assert while_held is None
    holder.close()
    with postgres_record.turn("site", "testing") as after:
        assert after is not None


def test_a_turn_is_a_session_lock_on_a_connection_of_its_own_with_no_transaction_open(postgres_record: PostgresRecord) -> None:
    with postgres_record.turn("site", "testing"), postgres_record.session("looking") as session:
        locks = session.execute("select count(*) as held from pg_locks where locktype = 'advisory' and granted").fetchone()
        idle = session.execute(
            "select count(*) as open from pg_stat_activity where state like 'idle in transaction%' and pid <> pg_backend_pid()"
        ).fetchone()
    assert locks is not None and idle is not None
    assert (locks["held"], idle["open"]) == (1, 0)


def test_a_database_that_cannot_be_reached_is_said_as_such_and_not_waited_for_long() -> None:
    unreachable = PostgresRecord("postgresql://launchplane@127.0.0.1:1/launchplane", NO_SECRETS, seconds_to_reach=0.2)
    done = threading.Event()
    try:
        with pytest.raises(RecordError, match="could not be reached while writing a request"):
            unreachable.write(MOVE_B, CALLER, None)
        done.set()
    finally:
        unreachable.close()
    assert done.is_set()


def test_a_turn_whose_connection_was_lost_says_it_is_no_longer_held(record: Record) -> None:
    with record.turn("site", "testing") as held:
        assert held is not None and held() is True
        lose_every_turn(record)
        assert held() is False


def test_a_key_is_kept_only_as_a_digest_so_the_record_cannot_show_it(postgres_record: PostgresRecord) -> None:
    key = "Zx3-a-password-sent-as-a-key"
    first = postgres_record.write(MOVE_B, CALLER, key)
    with postgres_record.session("reading every row") as session:
        # noinspection SqlResolve
        stored = str(session.execute("select * from request").fetchall())
    assert (key in stored, postgres_record.write(MOVE_B, CALLER, key).id) == (False, first.id)


def test_a_write_lost_after_the_request_was_sent_is_said_to_be_unconfirmed_not_unwritten(
    postgres_record: PostgresRecord, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_new = record_module.write_new

    def write_then_lose_the_connection(
        session: record_module.Session, gate: record_module.Gate, asked: Asked, asked_by: str, key: str | None
    ) -> None:
        write_new(session, gate, asked, asked_by, key)
        raise psycopg.OperationalError("server closed the connection unexpectedly")

    monkeypatch.setattr(record_module, "write_new", write_then_lose_the_connection)
    with pytest.raises(Unconfirmed, match="server closed the connection"):
        postgres_record.write(MOVE_B, CALLER, None)


def test_an_attempt_and_its_event_written_while_a_request_is_being_read_do_not_break_the_reading(
    postgres_record: PostgresRecord, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = postgres_record.write(MOVE_B, CALLER, None)
    assert postgres_record.take_up(request.id) is not None
    execute = psycopg.Cursor[Any].execute
    worker = PostgresRecord(postgres_record.database, NO_SECRETS)
    assert worker.read(request.id) is not None
    written_meanwhile: list[int] = []

    def execute_after_a_new_attempt(
        cursor: psycopg.Cursor[Any], query: LiteralString, values: Sequence[object] | None = None, **options: bool | None
    ) -> psycopg.Cursor[Any]:
        if query == record_module.Sql.EVENTS and not written_meanwhile:
            written_meanwhile.append(request.id)
            attempt = begun(worker, request.id)
            worker.note(attempt, "start the instance site-testing-1", None)
        return execute(cursor, query, values, **options)

    monkeypatch.setattr(psycopg.Cursor, "execute", execute_after_a_new_attempt)
    try:
        kept = postgres_record.read(request.id)
    finally:
        worker.close()
    assert kept is not None
    assert (written_meanwhile, len(kept.attempts), kept.events) == ([request.id], 1, [])


def test_the_migration_to_key_digests_refuses_to_be_undone_and_leaves_a_repeated_key_answering_with_its_request(
    postgres_record: PostgresRecord,
) -> None:
    first = postgres_record.write(MOVE_B, CALLER, "one-release")
    with pytest.raises(NotImplementedError, match="cannot be given back"):
        command.downgrade(Config(ALEMBIC), "0001")
    assert postgres_record.write(MOVE_B, CALLER, "one-release").id == first.id


def test_a_key_answered_with_another_calls_request_answers_with_it_ever_after_and_writes_nothing(record: Record) -> None:
    asked = record.write(MOVE_B, CALLER, None)
    answered = record.write(MOVE_B, CALLER, "lost-reply")
    attempt = begun(record, asked.id)
    record.end(attempt, Stands.DONE, {"outcome": "switched"}, None)
    later = record.write(MOVE_C, CALLER, None)
    again = record.write(MOVE_B, CALLER, "lost-reply")
    with pytest.raises(Refused, match=f"request {asked.id}"):
        record.write(MOVE_C, CALLER, "lost-reply")
    assert (answered.id, again.id, again.stands, [each.id for each in record.unfinished()]) == (
        asked.id,
        asked.id,
        Stands.DONE,
        [later.id],
    )


def test_the_migration_to_one_waiting_request_a_lane_leaves_the_newest_waiting_and_ends_the_others_as_replaced_by_it(
    postgres_record: PostgresRecord,
) -> None:
    command.downgrade(Config(ALEMBIC), "0005")
    try:
        with psycopg.connect(postgres_record.database) as connection:
            # noinspection SqlResolve
            waiting = "insert into request (product, lane, work, asked_by, generation) values ('site', %s, 'retiring', 'a test', %s)"
            for lane, generation in (("testing", "g1"), ("testing", "g2"), ("other", "g3"), ("testing", "g4")):
                connection.execute(waiting, [lane, generation])
    finally:
        command.upgrade(Config(ALEMBIC), "head")
    stand = {each.id: (each.stands, each.replaced_by) for each in (*postgres_record.unfinished(), *postgres_record.recently_ended())}
    assert stand == {1: ("replaced", 4), 2: ("replaced", 4), 3: ("waiting", None), 4: ("waiting", None)}


def test_the_migration_to_generations_stops_while_a_move_begun_before_it_is_working_and_goes_on_once_it_is_settled(
    postgres_record: PostgresRecord,
) -> None:
    before = postgres_record.write(MOVE_B, CALLER, None)
    assert postgres_record.take_up(before.id) is not None
    command.downgrade(Config(ALEMBIC), "0003")
    try:
        with pytest.raises(RuntimeError, match="are working or need a person"):
            command.upgrade(Config(ALEMBIC), "head")
        with psycopg.connect(postgres_record.database) as connection:
            # noinspection SqlResolve
            connection.execute("update request set stands = 'failed', ended_at = now() where id = %s", [before.id])
    finally:
        command.upgrade(Config(ALEMBIC), "head")
    settled = postgres_record.read(before.id)
    assert settled is not None and (settled.stands, len(settled.generation)) == (Stands.FAILED, 36)


# noinspection SqlResolve
OLD_ROWS = """
insert into request (product, lane, work, build, asked_by, generation, stands, ended_at, error, cancel_asked_at, cancel_asked_by)
values ('site', 'testing', 'moving', 'site:b', 'a test', 'g1', 'failed', now(), 'The cancel won, and new could not be removed.',
        now(), 'a test'),
       ('site', 'other', 'retiring', null, 'a test', 'g2', 'working', null, null, null, null);
insert into attempt (request, ended_at, ended) values (1, now(), 'failed'), (2, now(), 'interrupted'), (2, null, null);
insert into event (request, attempt, did, error)
values (1, 1, 'retire new, to put the lane back', 'Docker refused'), (2, 2, 'remove the route of other.site.test', null);
"""


def test_the_migration_to_route_lines_leaves_an_ended_request_and_an_interrupted_attempt_shown_as_they_were_written(
    postgres_record: PostgresRecord,
) -> None:
    command.downgrade(Config(ALEMBIC), "0008")
    try:
        with psycopg.connect(postgres_record.database) as connection:
            connection.execute(OLD_ROWS)
    finally:
        command.upgrade(Config(ALEMBIC), "head")
    ended, interrupted = postgres_record.read(1), postgres_record.read(2)
    assert ended is not None and interrupted is not None
    [refused], [removed] = ended.events, interrupted.events
    assert (ended.release_verdict, refused.action, refused.outcome, lines_of(ended)) == (
        None,
        None,
        None,
        ["retire new, to put the lane back, refused"],
    )
    not_kept = f"{ended.error} What the gate answered for its cancel was not kept."
    assert request_note(ended, {}) == f"{not_kept} Its lines: {'; '.join(lines_of(ended))}."
    assert (removed.is_credited, removed.is_unanswered, lines_of(interrupted)) == (False, False, [removed.did])
    assert [each.id for each in postgres_record.ended_lately()] == [1]
    attempt = begun(postgres_record, 2)
    postgres_record.complete(attempt, postgres_record.about_to(attempt, "remove the route", Sent(web_address="x", to=None)), Reply())
    taken_up = postgres_record.read(2)
    assert taken_up is not None and [each.ended for each in taken_up.attempts] == ["interrupted", "interrupted", None]
    assert [each.is_unanswered for each in taken_up.events] == [False, True]


def test_the_migration_that_ends_the_limit_leaves_a_request_that_needed_a_person_to_be_taken_up_again(
    postgres_record: PostgresRecord,
) -> None:
    parked = postgres_record.write(MOVE_B, CALLER, None)
    assert postgres_record.take_up(parked.id) is not None
    command.downgrade(Config(ALEMBIC), "0007")
    try:
        with psycopg.connect(postgres_record.database) as connection:
            # noinspection SqlResolve
            connection.execute("update request set stands = 'needs a person', error = 'Ask again.' where id = %s", [parked.id])
    finally:
        command.upgrade(Config(ALEMBIC), "head")
    taken_up_again = postgres_record.take_up(postgres_record.next_up()[0].id)
    kept = postgres_record.read(parked.id)
    assert kept is not None and taken_up_again is not None
    assert (kept.stands, kept.error, [each.ended for each in kept.attempts]) == (Stands.WORKING, None, ["interrupted", None])


HELD = "Zx3-a-secret-a-container-holds-0123456789"
ASKED_WITH_IT = Asked(product="site", lane="other", work=Work.MOVE, build=f"site:{HELD}", delete_data_set=HELD)


EVERY_ROW_OF_EVERY_TABLE = """
select string_agg(query_to_xml(format('select * from %I', table_name), true, true, '')::text, '')
from information_schema.tables where table_schema = 'public'
"""


# Every table and column there is, so one a later step adds is looked through too.
def everything_kept(record: Record) -> str:
    if not isinstance(record, PostgresRecord):
        return repr(record)
    with psycopg.connect(record.database) as connection:
        return repr(connection.execute(EVERY_ROW_OF_EVERY_TABLE).fetchall())


def emptied(record: Record) -> None:
    if isinstance(record, PretendRecord):
        record.kept.clear()
        record.keys.clear()
        record.open_attempts.clear()
        record.attempts_begun = 0
    if isinstance(record, PostgresRecord):
        with psycopg.connect(record.database, autocommit=True) as connection:
            # noinspection SqlResolve
            connection.execute("truncate request, attempt, event, request_key restart identity")


# Every kind of value a call of the record takes. One the test cannot fill fails it, so a new kind is given the secret too.
def filled(method: str, parameter: str, hint: object, number: int) -> object:
    kinds = {hint, *get_args(hint)}
    if hint is Asked:
        return ASKED_WITH_IT
    if hint is Stands:
        return Stands.DONE
    if hint is int:
        return number
    if hint is bool:
        return True
    if Verdict in kinds:
        return Verdict.COMMITTED
    if Sent in kinds:
        return Sent(web_address=HELD, to=f"to {HELD}")
    if hint is Reply:
        return Reply(made=True)
    if str in kinds:
        return f"said with {HELD} in it"
    if any(get_origin(kind) is dict for kind in kinds):
        return {f"named {HELD}": [HELD, {"deep": f"within {HELD}"}]}
    raise AssertionError(f"This test does not know how to fill {parameter} of {method}, so it could not give it the secret.")


def called_with_the_secret_everywhere(record: Record, method: str) -> None:
    emptied(record)
    resting(record, record.write(MOVE_B, CALLER, None).id)
    assert record.take_up(record.write(RETIRE_OTHER, CALLER, None).id) is not None
    hints = get_type_hints(getattr(Record, method))
    for number in range(1, 5):
        given = {name: filled(method, name, hint, number) for name, hint in hints.items() if name != "return"}
        try:
            answer = getattr(record, method)(**given)
            if isinstance(answer, AbstractContextManager):
                with answer:
                    pass
        except CarriesASecret, Refused, Interrupted:
            pass


def calls_that_keep_the_secret(record: Record) -> set[str]:
    calls = [name for name, member in vars(Record).items() if callable(member) and not name.startswith("_")]
    kept: set[str] = set()
    for call in calls:
        called_with_the_secret_everywhere(record, call)
        if HELD in everything_kept(record):
            kept.add(call)
    return kept


def test_no_call_of_the_record_keeps_a_secret_a_container_holds_in_any_table_whatever_argument_carries_it(
    record: Record, secrets: GivenSecrets
) -> None:
    while_nobody_holds_it = calls_that_keep_the_secret(record)
    secrets.seen = frozenset({HELD})
    while_a_container_holds_it = calls_that_keep_the_secret(record)
    assert while_nobody_holds_it >= {"write", "note", "end"}
    assert while_a_container_holds_it == set()


def test_a_statement_given_values_the_gate_has_not_judged_is_not_run(postgres_record: PostgresRecord) -> None:
    unjudged: Any = [HELD]
    with pytest.raises(AttributeError, match="values"), postgres_record.session("writing around the gate") as session:
        session.execute(record_module.Sql.SAME_KEY, unjudged)


@pytest.mark.parametrize("repeated", ["one-release", None], ids=["with a key", "with no key"])
def test_a_repeated_call_whose_lookup_was_lost_is_said_to_be_unconfirmed_since_its_request_is_written(
    postgres_record: PostgresRecord, monkeypatch: pytest.MonkeyPatch, repeated: str | None
) -> None:
    first = postgres_record.write(MOVE_B, CALLER, repeated)
    same_as_written = record_module.same_as_written

    def find_it_then_lose_the_connection(
        session: record_module.Session, gate: record_module.Gate, asked: Asked, key: str | None
    ) -> None:
        assert same_as_written(session, gate, asked, key) == first
        raise psycopg.OperationalError("server closed the connection unexpectedly")

    monkeypatch.setattr(record_module, "same_as_written", find_it_then_lose_the_connection)
    with pytest.raises(Unconfirmed, match="server closed the connection"):
        postgres_record.write(MOVE_B, CALLER, repeated)


def test_a_reading_the_record_does_not_answer_in_time_is_given_up_on_and_said_as_such(postgres_record: PostgresRecord) -> None:
    migrated_database = postgres_record.database
    record = PostgresRecord(migrated_database, NO_SECRETS, seconds_to_reach=0.3)
    began = time.monotonic()
    try:
        with psycopg.connect(migrated_database) as holder:
            # noinspection SqlResolve
            holder.execute("lock table request in access exclusive mode")
            with pytest.raises(RecordError, match=r"did not answer within 0.6 s while reading the requests not finished"):
                record.unfinished()
            waited = time.monotonic() - began
        assert (waited < 2, record.unfinished()) == (True, [])
    finally:
        record.close()
