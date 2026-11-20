from alembic import op

revision = "0007"
down_revision = "0006"

# Every key a request was answered for, as its digest: the key of the call that wrote it, and of each later call that
# asked the same while it was unfinished.
KEYS = "create table request_key (digest text primary key, request bigint not null references request)"
MOVE = "insert into request_key (digest, request) select key_digest, id from request where key_digest is not null"
# The older record keeps one key for a request, so every other key a request was answered for is forgotten.
BACK = """
update request set key_digest = one.digest
from (select request, min(digest) as digest from request_key group by request) one where request.id = one.request
"""


def upgrade() -> None:
    op.execute(KEYS)
    op.execute("create index on request_key (request)")
    op.execute(MOVE)
    op.execute("alter table request drop column key_digest")


def downgrade() -> None:
    op.execute("alter table request add column key_digest text unique")
    op.execute(BACK)
    op.execute("drop table request_key")
