"""Repair databases that reached 050 through the archived api chain.

Revision ID: 053_repair_api_lineage
Revises: 052_receipt_mode_build

The archived agent-middleware-api chain shares revision ids 047-050 with this
chain but never ran this repo's 044-046, so a database stamped there arrives
at 050 without receipt_resolutions, resolution_chain_heads,
approval_gate_approvals, api_key_usage_events and the
api_key_usage_daily view. This revision creates each one only when it is
absent, with exactly the columns, indexes and constraints of 044-046. It
writes no rows. On a database built from this chain it changes nothing.

Downgrade is a deliberate no-op: once applied, nothing in the schema records
whether 053 or 044-046 created an object, so dropping any of them could
destroy tables a fresh chain owns. Rolling back 044-046 remains their job.
"""

import sqlalchemy as sa
from alembic import op

revision: str = "053_repair_api_lineage"
down_revision: str = "052_receipt_mode_build"
branch_labels = None
depends_on = None

# Copied verbatim from 046_api_key_usage_events.
CREATE_API_KEY_USAGE_DAILY_VIEW_SQL = """
CREATE VIEW api_key_usage_daily AS
WITH ranked AS (
    SELECT
        key_id,
        DATE(created_at) AS day,
        status_code,
        latency_ms,
        created_at,
        ROW_NUMBER() OVER (
            PARTITION BY key_id, DATE(created_at)
            ORDER BY latency_ms, id
        ) AS latency_rank,
        COUNT(*) OVER (
            PARTITION BY key_id, DATE(created_at)
        ) AS call_count
    FROM api_key_usage_events
), positioned AS (
    SELECT
        *,
        ((call_count - 1) * 50) / 100 + 1 AS p50_floor_rank,
        ((call_count - 1) * 50) % 100 AS p50_remainder,
        ((call_count - 1) * 95) / 100 + 1 AS p95_floor_rank,
        ((call_count - 1) * 95) % 100 AS p95_remainder
    FROM ranked
)
SELECT
    key_id,
    day,
    COUNT(*) AS calls,
    SUM(CASE WHEN status_code BETWEEN 400 AND 499 THEN 1 ELSE 0 END) AS calls_4xx,
    SUM(CASE WHEN status_code BETWEEN 500 AND 599 THEN 1 ELSE 0 END) AS calls_5xx,
    SUM(CASE WHEN latency_rank = p50_floor_rank THEN latency_ms * (100 - p50_remainder) / 100.0 WHEN latency_rank = p50_floor_rank + 1 THEN latency_ms * p50_remainder / 100.0 ELSE 0 END) AS p50_latency_ms,
    SUM(CASE WHEN latency_rank = p95_floor_rank THEN latency_ms * (100 - p95_remainder) / 100.0 WHEN latency_rank = p95_floor_rank + 1 THEN latency_ms * p95_remainder / 100.0 ELSE 0 END) AS p95_latency_ms,
    MIN(created_at) AS first_call_at,
    MAX(created_at) AS last_call_at
FROM positioned
GROUP BY key_id, day
"""


def _resolution_chain_heads() -> None:
    op.create_table(
        "resolution_chain_heads",
        sa.Column("wallet_id", sa.String(length=50), primary_key=True),
        sa.Column("last_sequence", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_hash", sa.String(length=64), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["wallet_id"], ["wallets.wallet_id"]),
        sa.CheckConstraint(
            "last_sequence >= 0", name="ck_resolution_chain_heads_sequence"
        ),
    )


def _receipt_resolutions() -> None:
    op.create_table(
        "receipt_resolutions",
        sa.Column("resolution_id", sa.String(length=64), primary_key=True),
        sa.Column("receipt_id", sa.String(length=64), nullable=False),
        sa.Column("wallet_id", sa.String(length=50), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("prev_hash", sa.String(length=64), nullable=True),
        sa.Column("receipt_hash", sa.String(length=64), nullable=False),
        sa.Column("outcome", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("resolution_hash", sa.String(length=64), nullable=False),
        sa.Column("signing_input", sa.Text(), nullable=False),
        sa.Column("signature", sa.String(), nullable=False),
        sa.Column("signature_key_id", sa.String(length=64), nullable=False),
        sa.ForeignKeyConstraint(["receipt_id"], ["receipts.receipt_id"]),
        sa.ForeignKeyConstraint(["wallet_id"], ["wallets.wallet_id"]),
        sa.ForeignKeyConstraint(["signature_key_id"], ["signing_keys.key_id"]),
        sa.UniqueConstraint(
            "wallet_id", "sequence", name="uq_receipt_resolutions_wallet_seq"
        ),
        sa.CheckConstraint("sequence >= 1", name="ck_receipt_resolutions_sequence"),
        sa.CheckConstraint(
            "outcome IN ('confirmed_success', 'confirmed_not_delivered')",
            name="ck_receipt_resolutions_outcome",
        ),
    )
    for column in (
        "receipt_id",
        "wallet_id",
        "receipt_hash",
        "resolution_hash",
        "signature_key_id",
    ):
        op.create_index(
            f"ix_receipt_resolutions_{column}",
            "receipt_resolutions",
            [column],
            unique=column in {"receipt_id", "resolution_hash"},
        )


def _approval_gate_approvals() -> None:
    op.create_table(
        "approval_gate_approvals",
        sa.Column("nonce", sa.String(length=64), primary_key=True),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("args_hash", sa.String(length=128), nullable=False),
        sa.Column("principal", sa.String(length=255), nullable=False),
        sa.Column("tool", sa.String(length=128), nullable=False),
        sa.Column("session_id", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("deadline", sa.DateTime(), nullable=False),
        sa.Column("decided_at", sa.DateTime(), nullable=True),
        sa.Column("consumed_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'denied', 'expired')",
            name="ck_approval_gate_approvals_status",
        ),
    )
    for column in ("idempotency_key", "status"):
        op.create_index(
            f"ix_approval_gate_approvals_{column}",
            "approval_gate_approvals",
            [column],
            unique=False,
        )


def _api_key_usage_events() -> None:
    op.create_table(
        "api_key_usage_events",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("key_id", sa.String(length=50), nullable=False),
        sa.Column("tenant", sa.String(length=32), nullable=True),
        sa.Column("method", sa.String(length=16), nullable=False),
        sa.Column("endpoint", sa.String(length=512), nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index(
        "ix_api_key_usage_events_key_created",
        "api_key_usage_events",
        ["key_id", "created_at"],
    )
    op.create_index(
        "ix_api_key_usage_events_created", "api_key_usage_events", ["created_at"]
    )


# Creation order respects foreign keys; each runs only if its table is absent.
TABLES = (
    ("resolution_chain_heads", _resolution_chain_heads),
    ("receipt_resolutions", _receipt_resolutions),
    ("approval_gate_approvals", _approval_gate_approvals),
    ("api_key_usage_events", _api_key_usage_events),
)


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    for name, create in TABLES:
        if name not in tables:
            create()
    if "api_key_usage_daily" not in set(inspector.get_view_names()):
        op.execute(CREATE_API_KEY_USAGE_DAILY_VIEW_SQL)


def downgrade() -> None:
    """No-op by design; see the module docstring."""
