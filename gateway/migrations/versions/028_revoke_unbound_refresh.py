"""Revoke refresh tokens that cannot be bound to their minting API key.

Revision 025 added ``refresh_tokens.key_id`` but left historical rows NULL.
Wallet-level fallback is unsafe when a revoked key has a live sibling: an
unbound token could rotate forever through that sibling. The application now
fails closed on NULL bindings, and this data migration durably revokes every
historical row that cannot be attributed to a key.

Revision ID: 028_revoke_unbound_refresh
Revises: 027_governed_mcp_identity
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "028_revoke_unbound_refresh"
down_revision: Union[str, None] = "027_governed_mcp_identity"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE refresh_tokens SET revoked = :revoked WHERE key_id IS NULL"
        ).bindparams(revoked=True)
    )


def downgrade() -> None:
    # Revocation is intentionally irreversible: the prior state cannot
    # distinguish tokens that were already revoked from those revoked here.
    pass
