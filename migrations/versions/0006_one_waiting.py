from alembic import op

revision = "0006"
down_revision = "0005"

# Whatever writes a request, the record itself refuses a lane a second one that waits.
ONE_WAITING = "create unique index request_one_waiting on request (product, lane) where stands = 'waiting'"


def upgrade() -> None:
    op.execute(ONE_WAITING)


def downgrade() -> None:
    op.execute("drop index request_one_waiting")
