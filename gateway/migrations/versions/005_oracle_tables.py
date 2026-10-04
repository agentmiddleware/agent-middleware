"""Add oracle_* tables

Revision ID: 005_oracle_tables
Revises: 004_telemetry_events
Create Date: 2026-04-17

Replaces OracleStore's four in-memory collections (crawl targets,
indexed APIs, registrations, discovery hits) with persistent tables.

See issue #29.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "005_oracle_tables"
down_revision: Union[str, None] = "004_telemetry_events"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "oracle_crawl_targets",
        sa.Column("target_id", sa.String(64), primary_key=True),
        sa.Column("url", sa.String(2048), nullable=False, index=True),
        sa.Column("directory_type", sa.String(30), nullable=False, index=True),
        sa.Column("status", sa.String(20), nullable=False, index=True),
        sa.Column("api_id", sa.String(64), nullable=True, index=True),
        sa.Column(
            "queued_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
            index=True,
        ),
        sa.Column("crawled_at", sa.DateTime(), nullable=True),
    )

    op.create_table(
        "oracle_indexed_apis",
        sa.Column("api_id", sa.String(64), primary_key=True),
        sa.Column("url", sa.String(2048), nullable=False, index=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.String(2000), nullable=False, server_default=""),
        sa.Column("directory_type", sa.String(30), nullable=False, index=True),
        sa.Column("compatibility_tier", sa.String(20), nullable=False, index=True),
        sa.Column(
            "compatibility_score",
            sa.Float(),
            nullable=False,
            server_default="0.0",
            index=True,
        ),
        sa.Column("capabilities_json", sa.Text(), nullable=True),
        sa.Column("tags_json", sa.Text(), nullable=True),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column(
            "last_crawled",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
            index=True,
        ),
    )

    op.create_table(
        "oracle_registrations",
        sa.Column("registration_id", sa.String(64), primary_key=True),
        sa.Column("directory_url", sa.String(2048), nullable=False, index=True),
        sa.Column("directory_type", sa.String(30), nullable=False, index=True),
        sa.Column("status", sa.String(20), nullable=False, index=True),
        sa.Column("message", sa.String(2000), nullable=False, server_default=""),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
            index=True,
        ),
    )

    op.create_table(
        "oracle_discovery_hits",
        sa.Column("hit_id", sa.String(64), primary_key=True),
        sa.Column(
            "referrer",
            sa.String(2048),
            nullable=False,
            server_default="direct",
            index=True,
        ),
        sa.Column(
            "timestamp",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
            index=True,
        ),
    )


def downgrade() -> None:
    op.drop_table("oracle_discovery_hits")
    op.drop_table("oracle_registrations")
    op.drop_table("oracle_indexed_apis")
    op.drop_table("oracle_crawl_targets")
