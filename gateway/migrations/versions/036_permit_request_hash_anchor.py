"""permit request hash anchor

Revision ID: 036_permit_request_hash_anchor
Revises: 035_api_key_max_uses
Create Date: 2026-08-25

Add immutable anchor field for permit request hash integrity.
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "036_permit_request_hash_anchor"
down_revision = "035_api_key_max_uses"
branch_label = None
depends_on = None


def upgrade():
    """Add original_request_hash column to permit_requests.

    The new column stores the hash computed at request time and is never
    updated. Minting verifies that request_hash matches original_request_hash
    to prevent an attacker from coherently tampering with both the terms and
    the hash.
    """
    # SQLite doesn't support ALTER COLUMN, so we add the column as nullable,
    # backfill it, then recreate the table with the NOT NULL constraint.
    conn = op.get_bind()
    dialect_name = conn.dialect.name

    if dialect_name == "sqlite":
        # SQLite path: add nullable, backfill, then recreate table.
        op.add_column(
            "permit_requests",
            sa.Column("original_request_hash", sa.String(length=64), nullable=True),
        )
        op.execute(
            "UPDATE permit_requests SET original_request_hash = request_hash "
            "WHERE original_request_hash IS NULL"
        )
        # SQLite requires a full table recreation to add NOT NULL.
        # Since this is a new column with no existing production data that
        # differs from the backfilled value, we accept nullable in SQLite
        # and enforce the constraint in the service layer.
    else:
        # PostgreSQL path: add nullable, backfill, then set NOT NULL.
        op.add_column(
            "permit_requests",
            sa.Column("original_request_hash", sa.String(length=64), nullable=True),
        )
        op.execute(
            "UPDATE permit_requests SET original_request_hash = request_hash "
            "WHERE original_request_hash IS NULL"
        )
        op.alter_column("permit_requests", "original_request_hash", nullable=False)


def downgrade():
    op.drop_column("permit_requests", "original_request_hash")
