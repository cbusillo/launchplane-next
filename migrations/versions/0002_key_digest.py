from alembic import op

revision = "0002"
down_revision = "0001"

# A caller's key is kept only as its digest, so a key that was a secret cannot be read from the record.
DIGEST = "update request set idempotency_key = encode(sha256(convert_to(idempotency_key, 'UTF8')), 'hex')"
RENAME = "alter table request rename column idempotency_key to key_digest"


def upgrade() -> None:
    op.execute(DIGEST)
    op.execute(RENAME)


def downgrade() -> None:
    op.execute("alter table request rename column key_digest to idempotency_key")
