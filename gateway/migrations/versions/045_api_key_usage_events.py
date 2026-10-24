"""Store per-key API call facts and expose daily aggregates.

Revision ID: 045_api_key_usage_events
Revises: 044_receipt_resolutions

The event rows contain route templates only, never request payloads, raw URLs,
headers, client addresses, or credentials. Timestamps are naive UTC.
"""

from alembic import op
import sqlalchemy as sa

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

revision: str = "045_api_key_usage_events"
down_revision: str = "044_receipt_resolutions"
branch_labels = None
depends_on = None


def upgrade() -> None:
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

    op.execute(CREATE_API_KEY_USAGE_DAILY_VIEW_SQL)


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS api_key_usage_daily")
    op.drop_index(
        "ix_api_key_usage_events_key_created", table_name="api_key_usage_events"
    )
    op.drop_table("api_key_usage_events")
