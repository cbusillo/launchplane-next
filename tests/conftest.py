import os
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from alembic import command
from alembic.config import Config

from launchplane.record import Gate, PostgresRecord, Record
from tests.given_secrets import GivenSecrets
from tests.pretend_record import PretendRecord

TEST_DATABASE = os.environ.get("LAUNCHPLANE_TEST_DATABASE", "")
ALEMBIC = Path(__file__).parent.parent / "alembic.ini"


@pytest.fixture(scope="session")
def migrated_database() -> str:
    if not TEST_DATABASE:
        pytest.skip(
            "needs a PostgreSQL to write to: set LAUNCHPLANE_TEST_DATABASE, e.g. postgresql://postgres:test@127.0.0.1:55432/postgres"
        )
    os.environ["LAUNCHPLANE_DATABASE"] = TEST_DATABASE
    command.upgrade(Config(ALEMBIC), "head")
    return TEST_DATABASE


# The secrets the record's gate knows. A test changes what is seen to say what containers hold at that moment.
@pytest.fixture
def secrets() -> GivenSecrets:
    return GivenSecrets()


@pytest.fixture
def postgres_record(migrated_database: str, secrets: GivenSecrets) -> Iterator[PostgresRecord]:
    with psycopg.connect(migrated_database, autocommit=True) as connection:
        connection.execute("truncate request, attempt, event restart identity")
    record = PostgresRecord(migrated_database, secrets)
    yield record
    record.close()


# Each test of what the record does is run against the pretend one and against PostgreSQL, so the two cannot drift apart.
@pytest.fixture(params=["pretend", "postgres"])
def record(request: pytest.FixtureRequest, secrets: GivenSecrets) -> Record:
    if request.param == "pretend":
        return PretendRecord(gate=Gate(secrets))
    return request.getfixturevalue("postgres_record")


# A request left open is taken up again after a while. No test waits for that while.
@pytest.fixture(autouse=True)
def taken_up_again_at_once(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("launchplane.worker.SECONDS_BEFORE_TAKING_UP_AGAIN", 0)
