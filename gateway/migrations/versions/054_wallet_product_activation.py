"""Account-level product activation claim on wallets.

Revision ID: 054_wallet_product_activation
Revises: 053_repair_api_lineage

Adds nullable ``wallets.product_activated_at``. The gateway sets it once per
root wallet with a conditional UPDATE (``... IS NULL``) on a key's first use,
which makes the PostHog ``activation`` event fire once per account even when
first uses race.

Backfill: a root wallet whose own keys, or any descendant's keys (to the same
``MAX_WALLET_DEPTH`` the runtime resolver uses), were already used gets the
earliest such ``first_used_at``, so accounts active before 054 never emit a
late activation. One recursive CTE: api_keys is aggregated once per wallet,
then ancestry is walked via the wallets primary key (no per-root scan). The column is added
only when missing, and the backfill only touches NULL rows.
"""

import sqlalchemy as sa
from alembic import op

revision: str = "054_wallet_product_activation"
down_revision: str = "053_repair_api_lineage"
branch_labels = None
depends_on = None

# Must match app.services.api_key_service._MAX_WALLET_DEPTH (asserted by a test).
MAX_WALLET_DEPTH = 64

BACKFILL_SQL = f"""
WITH RECURSIVE used AS (
    SELECT wallet_id, MIN(first_used_at) AS first_used_at
    FROM api_keys
    WHERE first_used_at IS NOT NULL
    GROUP BY wallet_id
),
anc (wallet_id, ancestor_id, depth) AS (
    SELECT wallet_id, wallet_id, 0 FROM used
    UNION ALL
    SELECT a.wallet_id, w.parent_wallet_id, a.depth + 1
    FROM anc a
    JOIN wallets w ON w.wallet_id = a.ancestor_id
    WHERE w.parent_wallet_id IS NOT NULL AND a.depth < {MAX_WALLET_DEPTH}
),
root_first AS (
    SELECT a.ancestor_id AS root_id, MIN(u.first_used_at) AS first_used_at
    FROM anc a
    JOIN used u ON u.wallet_id = a.wallet_id
    JOIN wallets r ON r.wallet_id = a.ancestor_id AND r.parent_wallet_id IS NULL
    GROUP BY a.ancestor_id
)
UPDATE wallets SET product_activated_at = (
    SELECT rf.first_used_at FROM root_first rf WHERE rf.root_id = wallets.wallet_id
)
WHERE parent_wallet_id IS NULL
  AND product_activated_at IS NULL
  AND wallet_id IN (SELECT root_id FROM root_first)
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
