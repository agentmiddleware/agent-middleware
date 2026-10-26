"""Account-level product activation claim on wallets.

Revision ID: 054_wallet_product_activation
Revises: 053_repair_api_lineage

Adds nullable ``wallets.product_activated_at``. The gateway sets it once per
root wallet with a conditional UPDATE (``... IS NULL``) on a key's first use,
which makes the PostHog ``activation`` event fire once per account even when
first uses race.

Backfill: a root wallet whose own keys, or whose descendants' keys (up to
three levels), were already used gets the earliest such ``first_used_at``, so
accounts active before 054 never emit a late activation. The column is added
only when missing, and the backfill only touches NULL rows.
"""

import sqlalchemy as sa
from alembic import op

revision: str = "054_wallet_product_activation"
down_revision: str = "053_repair_api_lineage"
branch_labels = None
depends_on = None

BACKFILL_SQL = """
UPDATE wallets SET product_activated_at = (
    SELECT MIN(k.first_used_at)
    FROM api_keys k
    JOIN wallets w1 ON w1.wallet_id = k.wallet_id
    LEFT JOIN wallets w2 ON w2.wallet_id = w1.parent_wallet_id
    LEFT JOIN wallets w3 ON w3.wallet_id = w2.parent_wallet_id
    WHERE k.first_used_at IS NOT NULL
      AND wallets.wallet_id IN (w1.wallet_id, w1.parent_wallet_id,
                                w2.parent_wallet_id, w3.parent_wallet_id)
)
WHERE parent_wallet_id IS NULL AND product_activated_at IS NULL
"""


def upgrade() -> None:
    existing = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("wallets")}
    if "product_activated_at" not in existing:
        with op.batch_alter_table("wallets") as batch_op:
            batch_op.add_column(
                sa.Column("product_activated_at", sa.DateTime(), nullable=True)
            )
    op.execute(sa.text(BACKFILL_SQL))


def downgrade() -> None:
    # Analytics-only marker; dropping it loses no signed or financial data.
    with op.batch_alter_table("wallets") as batch_op:
        batch_op.drop_column("product_activated_at")
