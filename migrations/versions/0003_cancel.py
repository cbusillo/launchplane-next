from alembic import op

revision = "0003"
down_revision = "0002"

STANDS = "alter table request drop constraint request_stands_check, add constraint request_stands_check check (stands in ({}))"
BEFORE = "'waiting', 'working', 'done', 'failed', 'needs a person'"
# The mark a worker reads between actions, who asked for it, and why it was not carried out when it was not.
MARK = "alter table request add column cancel_asked_at timestamptz, add column cancel_asked_by text, add column cancel_refused text"
UNMARK = "alter table request drop column cancel_asked_at, drop column cancel_asked_by, drop column cancel_refused"
# A request that was cancelled did not do what was asked, which the older record can only call failed.
AS_FAILED = "update request set stands = 'failed', error = 'Cancelled by ' || cancel_asked_by || '.' where stands = 'cancelled'"


def upgrade() -> None:
    op.execute(STANDS.format(f"{BEFORE}, 'cancelled'"))
    op.execute(MARK)


def downgrade() -> None:
    op.execute(AS_FAILED)
    op.execute(STANDS.format(BEFORE))
    op.execute(UNMARK)
