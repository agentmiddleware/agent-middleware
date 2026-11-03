"""Unique (permit_id, sequence) for opted-in receipt chains.

Revision ID: 057_receipt_permit_seq_unique
Revises: 056_receipt_digests_chain

Partial unique index so multiple NULL sequences (pre-056 / non-chain receipts)
remain allowed. Fail-closed when duplicate non-NULL (permit_id, sequence)
groups already exist: operators must adjudicate before the invariant can
install. Application SELECT checks stay as a fast path; this index closes the
concurrent-write race.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "057_receipt_permit_seq_unique"
down_revision: Union[str, None] = "056_receipt_digests_chain"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_INDEX_NAME = "uq_receipts_permit_sequence"


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        # Prevent writers from inserting a second (permit_id, sequence) between
        # the preflight count and index install. Matches 027 lock posture.
        op.execute(sa.text("LOCK TABLE receipts IN SHARE ROW EXCLUSIVE MODE"))

    ambiguous_groups = bind.scalar(
        sa.text(
            """
            SELECT COUNT(*)
            FROM (
                SELECT permit_id, sequence
                FROM receipts
                WHERE sequence IS NOT NULL
                GROUP BY permit_id, sequence
                HAVING COUNT(*) > 1
            ) AS duplicate_permit_sequences
            """
        )
    )
    if int(ambiguous_groups or 0):
        raise RuntimeError(
            "cannot install receipt sequence uniqueness: "
            f"{int(ambiguous_groups)} duplicate (permit_id, sequence) group(s); "
            "adjudicate before re-running"
        )

    if bind.dialect.name == "postgresql":
        existing = bind.execute(
            sa.text(
                "SELECT 1 FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE c.relkind = 'i' AND c.relname = :name "
                "AND n.nspname = current_schema()"
            ),
            {"name": _INDEX_NAME},
        ).fetchone()
        if existing is None:
            op.execute(
                sa.text(
                    f"CREATE UNIQUE INDEX {_INDEX_NAME} "
                    "ON receipts (permit_id, sequence) "
                    "WHERE sequence IS NOT NULL"
                )
            )
    else:
        # SQLite: IF NOT EXISTS keeps re-runs idempotent under create_all+alembic mixes.
        op.execute(
            sa.text(
                f"CREATE UNIQUE INDEX IF NOT EXISTS {_INDEX_NAME} "
                "ON receipts (permit_id, sequence) "
                "WHERE sequence IS NOT NULL"
            )
        )


def downgrade() -> None:
    op.drop_index(_INDEX_NAME, table_name="receipts")
