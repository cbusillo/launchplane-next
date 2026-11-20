from alembic import op

revision = "0006"
down_revision = "0005"

# Held to the end of the migration, so no request is written between the tidying and the index.
LOCK = "lock table request in share row exclusive mode"
# Until now a lane could have several requests waiting. All but the newest end as replaced by it, so the index can be made.
KEEP_THE_NEWEST = """
update request older set stands = 'replaced', ended_at = now(), replaced_by = newest.id
from (select product, lane, max(id) as id from request where stands = 'waiting' group by product, lane) newest
where (older.product, older.lane) = (newest.product, newest.lane) and older.stands = 'waiting' and older.id < newest.id
"""
# Whatever writes a request, the record itself refuses a lane a second one that waits.
ONE_WAITING = "create unique index request_one_waiting on request (product, lane) where stands = 'waiting'"


def upgrade() -> None:
    op.execute(LOCK)
    op.execute(KEEP_THE_NEWEST)
    op.execute(ONE_WAITING)


def downgrade() -> None:
    op.execute("drop index request_one_waiting")
