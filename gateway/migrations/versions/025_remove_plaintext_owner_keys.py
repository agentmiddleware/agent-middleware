"""Retire plaintext API-key ownership values without breaking old workers.

Revision ID: 025_remove_plaintext_owner_keys
Revises: 025_refresh_token_key_binding
Create Date: 2026-08-05

Wallet and service ownership is represented by wallet identifiers and the
hashed API-key registry. The legacy ``owner_key`` columns duplicated live API
credentials in plaintext and are not used by current code.

This revision deliberately retains the columns and indexes for one rolling
release. Workers from the previous release still select and write these
columns; dropping them before those workers drain causes query failures. The
migration performs an initial scrub, while the deploy workflow performs the
same idempotent scrub and assertion again after Railway reports that only the
new deployment is active. A later contract migration may drop the empty
compatibility columns once no old binary can be running.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "025_remove_plaintext_owner_keys"
down_revision: Union[str, None] = "025_refresh_token_key_binding"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Scrub legacy credentials while retaining the rolling-compatible shape."""
    op.execute(
        sa.text(
            """
            UPDATE wallets
            SET owner_key = ''
            WHERE owner_key IS NULL OR owner_key <> ''
            """
        )
    )
    # Current workers no longer map these columns, so the retained NOT NULL
    # shape needs an empty server default for their inserts. Previous workers
    # continue to supply a value explicitly and remain schema-compatible.
    with op.batch_alter_table("wallets") as batch_op:
        batch_op.alter_column(
            "owner_key",
            existing_type=sa.String(length=255),
            existing_nullable=False,
            server_default="",
        )
    with op.batch_alter_table("service_registry") as batch_op:
        batch_op.alter_column(
            "owner_key",
            existing_type=sa.String(length=255),
            existing_nullable=False,
            server_default="",
        )
    op.execute(
        sa.text(
            """
            UPDATE service_registry
            SET owner_key = ''
            WHERE owner_key IS NULL OR owner_key <> ''
            """
        )
    )


def downgrade() -> None:
    """Keep the compatibility shape; scrubbed credentials are unrecoverable."""
    with op.batch_alter_table("service_registry") as batch_op:
        batch_op.alter_column(
            "owner_key",
            existing_type=sa.String(length=255),
            existing_nullable=False,
            server_default=None,
        )
    with op.batch_alter_table("wallets") as batch_op:
        batch_op.alter_column(
            "owner_key",
            existing_type=sa.String(length=255),
            existing_nullable=False,
            server_default=None,
        )
