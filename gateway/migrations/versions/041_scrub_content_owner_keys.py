"""Scrub plaintext API keys from content_pipelines / content_campaigns.

Revision ID: 041_scrub_content_owner_keys
Revises: 040_permit_repeat_window
Create Date: 2026-10-01

Until the factory routers started recording the caller's wallet id, every
pipeline and campaign stored the raw ``X-API-Key`` string that created it in
``owner_key``: a live credential at rest, and one the read paths never
compared against. The routers now write the owning wallet id and enforce it
on every read, so a legacy value is both a secret at rest and unmatchable by
any caller.

This revision blanks every ``owner_key`` that is not a known wallet id. Rows
written by the current code keep their owner. Legacy rows become ownerless,
which only bootstrap admins can read: the same fail-closed outcome they
already have, since no wallet id equals a raw key. The column shape is
unchanged, so previous and current workers stay schema-compatible, the same
rolling-release approach as 025_remove_plaintext_owner_keys.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "041_scrub_content_owner_keys"
down_revision: Union[str, None] = "040_permit_repeat_window"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLES = ("content_pipelines", "content_campaigns")


def upgrade() -> None:
    """Blank every owner_key that is not a wallet id; idempotent."""
    for table in _TABLES:
        op.execute(
            sa.text(
                f"""
                UPDATE {table}
                SET owner_key = ''
                WHERE owner_key <> ''
                  AND owner_key NOT IN (SELECT wallet_id FROM wallets)
                """
            )
        )


def downgrade() -> None:
    """Nothing to restore: the scrubbed values were credentials and are gone."""
