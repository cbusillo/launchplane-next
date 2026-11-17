import psycopg
from alembic import context
from sqlalchemy import create_engine, pool

from launchplane.record import conninfo

engine = create_engine("postgresql+psycopg://", creator=lambda: psycopg.connect(conninfo()), poolclass=pool.NullPool)
with engine.connect() as connection:
    context.configure(connection=connection)
    with context.begin_transaction():
        context.run_migrations()
