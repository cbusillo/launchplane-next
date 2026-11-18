from alembic import op

revision = "0004"
down_revision = "0003"

# Every request is one generation. A request written before generations gets one made from chance, so none repeats.
ADD = "alter table request add column generation text not null default gen_random_uuid()::text unique"
# From here on Launchplane gives each request its generation as it writes it.
NO_DEFAULT = "alter table request alter column generation drop default"


def upgrade() -> None:
    op.execute(ADD)
    op.execute(NO_DEFAULT)


def downgrade() -> None:
    op.execute("alter table request drop column generation")
