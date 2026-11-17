import threading
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractContextManager
from typing import Any, LiteralString, get_args, get_origin, get_type_hints

import psycopg
import pytest
from alembic import command
from alembic.config import Config

from launchplane import record as record_module
from launchplane.judge import Work
from launchplane.record import (
    MOST_ATTEMPTS,
    Asked,
    CarriesASecret,
    Interrupted,
    PostgresRecord,
    Record,
    RecordError,
    Refused,
    Stands,
    Unconfirmed,
)
from tests.conftest import ALEMBIC
from tests.given_secrets import NO_SECRETS, GivenSecrets
from tests.pretend_record import PretendRecord, lose_every_turn

MOVE_B = Asked(product="site", lane="testing", work=Work.MOVE, build="site:b")
MOVE_C = Asked(product="site", lane="testing", work=Work.MOVE, build="site:c")
RETIRE_OTHER = Asked(product="site", lane="other", work=Work.RETIRE)
CALLER = "the address 192.0.2.1"


def taken_up_until_it_needs_a_person(record: Record, request: int) -> None:
    for _ in range(MOST_ATTEMPTS):
        assert record.take_up(request) is not None
    assert record.take_up(request) is None


def test_asking_the_same_of_a_lane_while_one_is_unfinished_writes_no_second_request(record: Record) -> None:
    first = record.write(MOVE_B, CALLER, None)
    again = record.write(MOVE_B, "the address 192.0.2.2", None)
    other = record.write(MOVE_C, CALLER, None)
    assert (again.id, again.asked_by, other.id != first.id) == (first.id, CALLER, True)


def test_asking_the_same_again_once_the_first_has_ended_writes_a_new_request(record: Record) -> None:
    first = record.write(MOVE_B, CALLER, None)
    attempt = record.take_up(first.id)
    assert attempt is not None
    record.end(attempt, Stands.DONE, {"outcome": "switched"}, None)
    assert record.write(MOVE_B, CALLER, None).id != first.id


def test_the_same_key_and_body_answers_with_the_request_already_written_even_after_it_ended(record: Record) -> None:
    first = record.write(MOVE_B, CALLER, "key-1")
    attempt = record.take_up(first.id)
    assert attempt is not None
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


def test_requests_are_next_up_in_the_order_they_came(record: Record) -> None:
    first, second = record.write(MOVE_B, CALLER, None), record.write(RETIRE_OTHER, CALLER, None)
    order: list[int] = []
    for _ in range(2):
        up = record.next_up()
        assert up is not None
        order.append(up.id)
        attempt = record.take_up(up.id)
        assert attempt is not None
        record.end(attempt, Stands.DONE, None, None)
    assert (order, record.next_up()) == ([first.id, second.id], None)


def test_a_request_found_working_is_taken_up_as_interrupted_and_the_attempt_is_counted(record: Record) -> None:
    request = record.write(MOVE_B, CALLER, None)
    first = record.take_up(request.id)
    second = record.take_up(request.id)
    kept = record.read(request.id)
    assert kept is not None and first is not None and second is not None
    assert (kept.stands, [(each.number, each.ended) for each in kept.attempts]) == (
        Stands.WORKING,
        [(1, "interrupted"), (2, None)],
    )


def test_an_attempt_that_was_interrupted_writes_nothing_more(record: Record) -> None:
    request = record.write(MOVE_B, CALLER, None)
    first = record.take_up(request.id)
    second = record.take_up(request.id)
    assert first is not None and second is not None
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


def test_a_request_taken_up_three_times_without_ending_needs_a_person_and_says_what_to_do(record: Record) -> None:
    request = record.write(MOVE_B, CALLER, None)
    taken_up_until_it_needs_a_person(record, request.id)
    kept = record.read(request.id)
    assert kept is not None and kept.error is not None
    assert (kept.stands, [each.ended for each in kept.attempts]) == (Stands.NEEDS_A_PERSON, ["interrupted"] * MOST_ATTEMPTS)
    assert f"POST /requests/{request.id}/retry" in kept.error and f"POST /requests/{request.id}/give-up" in kept.error


def test_a_lane_whose_request_needs_a_person_takes_no_other_request_and_another_lane_still_does(record: Record) -> None:
    waiting_behind = record.write(MOVE_C, CALLER, "written-before-the-first-stopped")
    request = record.write(MOVE_B, CALLER, None)
    taken_up_until_it_needs_a_person(record, request.id)
    with pytest.raises(Refused, match="needs a person"):
        record.write(Asked(product="site", lane="testing", work=Work.RETIRE), CALLER, None)
    assert record.next_up() is None
    other = record.write(RETIRE_OTHER, CALLER, None)
    up = record.next_up()
    assert up is not None and (up.id, waiting_behind.stands) == (other.id, Stands.WAITING)


def test_giving_a_request_up_ends_it_as_failed_and_frees_its_lane(record: Record) -> None:
    request = record.write(MOVE_B, CALLER, None)
    taken_up_until_it_needs_a_person(record, request.id)
    given_up = record.give_up(request.id)
    assert given_up is not None and given_up.error is not None
    assert (given_up.stands, given_up.error.endswith("A person gave it up.")) == (Stands.FAILED, True)
    assert record.write(MOVE_C, CALLER, None).stands == Stands.WAITING


def test_asking_for_a_request_again_writes_a_new_request_for_the_same_thing_and_ends_the_old(record: Record) -> None:
    request = record.write(MOVE_B, CALLER, None)
    taken_up_until_it_needs_a_person(record, request.id)
    again = record.retry(request.id, "the address 192.0.2.9")
    old = record.read(request.id)
    assert again is not None and old is not None and old.error is not None
    assert (again.asked(), again.stands, again.asked_by) == (MOVE_B, Stands.WAITING, "the address 192.0.2.9")
    assert (old.stands, f"as request {again.id}" in old.error) == (Stands.FAILED, True)
    up = record.next_up()
    assert up is not None and up.id == again.id


def test_only_a_request_that_needs_a_person_is_given_up_or_asked_for_again(record: Record) -> None:
    request = record.write(MOVE_B, CALLER, None)
    assert (record.give_up(request.id), record.retry(request.id, CALLER), record.give_up(999)) == (None, None, None)
    kept = record.read(request.id)
    assert kept is not None and kept.stands == Stands.WAITING


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
        with pytest.raises(RecordError, match="could not be reached while writing a request") as refused:
            unreachable.write(MOVE_B, CALLER, None)
        assert not isinstance(refused.value, Unconfirmed)
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
            attempt = worker.take_up(request.id)
            assert attempt is not None
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
            connection.execute("truncate request, attempt, event restart identity")


# Every kind of value a call of the record takes. One the test cannot fill fails it, so a new kind is given the secret too.
def filled(method: str, parameter: str, hint: object, number: int) -> object:
    kinds = {hint, *get_args(hint)}
    if hint is Asked:
        return ASKED_WITH_IT
    if hint is Stands:
        return Stands.DONE
    if hint is int:
        return number
    if str in kinds:
        return f"said with {HELD} in it"
    if any(get_origin(kind) is dict for kind in kinds):
        return {f"named {HELD}": [HELD, {"deep": f"within {HELD}"}]}
    raise AssertionError(f"This test does not know how to fill {parameter} of {method}, so it could not give it the secret.")


def called_with_the_secret_everywhere(record: Record, method: str) -> None:
    emptied(record)
    stuck = record.write(MOVE_B, CALLER, None)
    taken_up_until_it_needs_a_person(record, stuck.id)
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
    assert while_nobody_holds_it >= {"write", "retry", "note", "end"}
    assert while_a_container_holds_it == set()


def test_a_statement_given_values_the_gate_has_not_judged_is_not_run(postgres_record: PostgresRecord) -> None:
    unjudged: Any = [HELD]
    with pytest.raises(AttributeError, match="values"), postgres_record.session("writing around the gate") as session:
        session.execute(record_module.Sql.SAME_KEY, unjudged)
