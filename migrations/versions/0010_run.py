from alembic import op

revision = "0010"
down_revision = "0009"


# The publishing run a move named. Its build is the image that run was verified to have published.
def upgrade() -> None:
    op.execute("alter table request add column run bigint")


def downgrade() -> None:
    op.execute("alter table request drop column run")
