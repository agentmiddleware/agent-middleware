"""Store per-key API call facts and expose daily aggregates.

Revision ID: 045_api_key_usage_events
Revises: 044_receipt_resolutions

The event rows contain route templates only, never request payloads, raw URLs,
headers, client addresses, or credentials. Timestamps are naive UTC.
"""

from alembic import op
import sqlalchemy as sa

from app.db.api_key_usage_view import CREATE_API_KEY_USAGE_DAILY_VIEW_SQL

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
