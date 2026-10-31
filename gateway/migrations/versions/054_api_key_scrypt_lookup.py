"""API keys: scrypt hashes found by an indexed lookup id.

Revision ID: 054_api_key_scrypt_lookup
Revises: 053_repair_api_lineage

Widens api_keys.key_hash to 255 characters for the encoded scrypt hash and
adds the nullable, indexed api_keys.key_lookup (the first 16 characters of
the key, non-secret). It writes no rows. Existing rows keep a NULL
key_lookup and their old SHA-256 digest, so they never authenticate again:
every key must be reissued after deploy (docs/api-key-hashing.md).

Downgrade drops the index and column and narrows key_hash back to 64. Run it
only after deleting rows whose key_hash is longer than 64 characters.
"""

import sqlalchemy as sa
from alembic import op

revision: str = "054_api_key_scrypt_lookup"
down_revision: str = "053_repair_api_lineage"
branch_labels = None
depends_on = None


def _is_sqlite() -> bool:
    return op.get_bind().dialect.name == "sqlite"


# SQLite does not enforce VARCHAR length, and a batch table rebuild would break
# the views that read api_keys, so the width change is skipped there.


def upgrade() -> None:
    if not _is_sqlite():
        op.alter_column(
            "api_keys",
            "key_hash",
            existing_type=sa.String(64),
            type_=sa.String(255),
            existing_nullable=False,
        )
    op.add_column("api_keys", sa.Column("key_lookup", sa.String(16), nullable=True))
    op.create_index("ix_api_keys_key_lookup", "api_keys", ["key_lookup"])


def downgrade() -> None:
    op.drop_index("ix_api_keys_key_lookup", table_name="api_keys")
    op.drop_column("api_keys", "key_lookup")
    if not _is_sqlite():
        op.alter_column(
            "api_keys",
            "key_hash",
            existing_type=sa.String(255),
            type_=sa.String(64),
            existing_nullable=False,
        )
