from alembic import op

revision = "0005"
down_revision = "0004"

STANDS = "alter table request drop constraint request_stands_check, add constraint request_stands_check check (stands in ({}))"
BEFORE = "'waiting', 'working', 'done', 'failed', 'needs a person', 'cancelled'"
# A request that was replaced did nothing, which the older record can only call cancelled by the request that replaced it.
AS_CANCELLED = """
update request set stands = 'cancelled', cancel_asked_at = ended_at, cancel_asked_by = 'request ' || replaced_by
where stands = 'replaced'
"""


def upgrade() -> None:
    op.execute(STANDS.format(f"{BEFORE}, 'replaced'"))
    op.execute("alter table request add column replaced_by bigint references request")


def downgrade() -> None:
    op.execute(AS_CANCELLED)
    op.execute(STANDS.format(BEFORE))
    op.execute("alter table request drop column replaced_by")
