from alembic import op

revision = "0001"
down_revision = None

REQUEST = """
create table request (
    id bigint generated always as identity primary key,
    product text not null,
    lane text not null,
    work text not null,
    build text,
    delete_data_set text,
    asked_by text not null,
    asked_at timestamptz not null default now(),
    idempotency_key text unique,
    stands text not null default 'waiting' check (stands in ('waiting', 'working', 'done', 'failed', 'needs a person')),
    ended_at timestamptz,
    answer jsonb,
    error text
)
"""
UNFINISHED = "create index request_unfinished on request (id) where stands in ('waiting', 'working', 'needs a person')"
ATTEMPT = """
create table attempt (
    id bigint generated always as identity primary key,
    request bigint not null references request,
    began_at timestamptz not null default now(),
    ended_at timestamptz,
    ended text
)
"""
EVENT = """
create table event (
    id bigint generated always as identity primary key,
    request bigint not null references request,
    attempt bigint not null references attempt,
    at timestamptz not null default now(),
    did text not null,
    error text
)
"""


def upgrade() -> None:
    for statement in (REQUEST, UNFINISHED, ATTEMPT, "create index on attempt (request)", EVENT, "create index on event (request)"):
        op.execute(statement)


def downgrade() -> None:
    for table in ("event", "attempt", "request"):
        op.execute(f"drop table {table}")
