from alembic import op

revision = "0008"
down_revision = "0007"

STANDS = "alter table request drop constraint request_stands_check, add constraint request_stands_check check (stands in ({}))"
NOW = "'waiting', 'working', 'done', 'failed', 'cancelled', 'replaced'"
# A request found working with nobody on its lane is taken up again, so one that was left to a person now is. Its error
# said how to ask for it again, which no call does any more.
TAKEN_UP_AGAIN = "update request set stands = 'working', error = null where stands = 'needs a person'"


def upgrade() -> None:
    op.execute(TAKEN_UP_AGAIN)
    op.execute(STANDS.format(NOW))


# No request is left to a person again: the older Launchplane does that itself, by its count.
def downgrade() -> None:
    op.execute(STANDS.format(f"{NOW}, 'needs a person'"))
