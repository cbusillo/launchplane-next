from alembic import op

revision = "0004"
down_revision = "0003"

# Every request is one generation. A request written before generations gets one made from chance, so none repeats.
ADD = "alter table request add column generation text not null default gen_random_uuid()::text unique"
# From here on Launchplane gives each request its generation as it writes it.
NO_DEFAULT = "alter table request alter column generation drop default"
# Held to the end of the migration, so no move is taken up between the look and the new column.
LOCK = "lock table request in share row exclusive mode"
# A move the Launchplane before generations began may already have switched, and the gate has nothing to say so: under
# a generation made from chance it would be finished as if it had not.
BEGUN_BEFORE = "select count(*) from request where work = 'moving' and stands in ('working', 'needs a person')"


def upgrade() -> None:
    op.execute(LOCK)
    begun = op.get_bind().exec_driver_sql(BEGUN_BEFORE).scalar_one()
    if begun:
        raise RuntimeError(
            f"{begun} move(s) are working or need a person, begun by the Launchplane before this migration. Let that "
            "Launchplane finish them, or retry or give up the ones that need a person with it, then migrate. Nothing was changed."
        )
    op.execute(ADD)
    op.execute(NO_DEFAULT)


def downgrade() -> None:
    op.execute("alter table request drop column generation")
