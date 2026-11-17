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
    raise NotImplementedError(
        "The keys cannot be given back: only their digests are kept. Going down and up again would digest them twice, and "
        "a caller repeating a key would then be written a second request. Nothing was changed."
    )
