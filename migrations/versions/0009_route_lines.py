from alembic import op

revision = "0009"
down_revision = "0008"

# A line or a request written before this has none of these, and stays as it was written.
EVENT = "alter table event add column action jsonb, add column outcome jsonb"
VERDICT = """
alter table request add column release_verdict text check (release_verdict in ('none', 'committed', 'abandoned'))
"""


def upgrade() -> None:
    op.execute(EVENT)
    op.execute(VERDICT)


def downgrade() -> None:
    op.execute("alter table event drop column action, drop column outcome")
    op.execute("alter table request drop column release_verdict")
