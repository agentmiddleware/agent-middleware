"""Add content_factory tables

Revision ID: 007_content_factory
Revises: 006_security_scan
Create Date: 2026-04-17

Replaces content_factory.ContentStore's three in-memory collections
with persistent tables. Also introduces content_schedules so
AlgorithmicScheduler recommendations can be persisted once the ML
scheduler lands (currently scheduler returns recommendations in-memory).

See issue #31.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "007_content_factory"
down_revision: Union[str, None] = "006_security_scan"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "content_pipelines",
        sa.Column("pipeline_id", sa.String(64), primary_key=True),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("source_clip_id", sa.String(100), nullable=True),
        sa.Column("source_url", sa.String(2048), nullable=True),
        sa.Column("target_formats_json", sa.Text(), nullable=True),
        sa.Column("brand_config_json", sa.Text(), nullable=True),
        sa.Column("language", sa.String(10), nullable=False, server_default="en"),
        sa.Column(
            "auto_schedule", sa.Boolean(), nullable=False, server_default=sa.true()
        ),
        sa.Column(
            "owner_key", sa.String(255), nullable=False, server_default="", index=True
        ),
        sa.Column(
            "status", sa.String(30), nullable=False, server_default="queued", index=True
        ),
        sa.Column("hook_json", sa.Text(), nullable=True),
        sa.Column(
            "caption_style",
            sa.String(30),
            nullable=False,
            server_default="bold_impact",
        ),
        sa.Column("aspect_ratio", sa.String(10), nullable=False, server_default="9:16"),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
            index=True,
        ),
    )

    op.create_table(
        "content_pieces",
        sa.Column("content_id", sa.String(64), primary_key=True),
        sa.Column(
            "pipeline_id",
            sa.String(64),
            sa.ForeignKey("content_pipelines.pipeline_id"),
            nullable=False,
            index=True,
        ),
        sa.Column("format", sa.String(30), nullable=False, index=True),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("description", sa.String(2000), nullable=True),
        sa.Column("download_url", sa.String(2048), nullable=False),
        sa.Column("thumbnail_url", sa.String(2048), nullable=True),
        sa.Column("duration_seconds", sa.Float(), nullable=True),
        sa.Column("dimensions", sa.String(30), nullable=True),
        sa.Column("file_size_bytes", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, index=True),
        sa.Column("metadata_json", sa.Text(), nullable=True),
        sa.Column(
            "generated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
            index=True,
        ),
    )

    op.create_table(
        "content_campaigns",
        sa.Column("campaign_id", sa.String(64), primary_key=True),
        sa.Column("campaign_title", sa.String(500), nullable=False),
        sa.Column("source_url", sa.String(2048), nullable=False),
        sa.Column("hooks_json", sa.Text(), nullable=True),
        sa.Column("pipeline_ids_json", sa.Text(), nullable=True),
        sa.Column(
            "status",
            sa.String(30),
            nullable=False,
            server_default="running",
            index=True,
        ),
        sa.Column(
            "owner_key", sa.String(255), nullable=False, server_default="", index=True
        ),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
            index=True,
        ),
    )

    op.create_table(
        "content_schedules",
        sa.Column("schedule_id", sa.String(64), primary_key=True),
        sa.Column(
            "content_id",
            sa.String(64),
            sa.ForeignKey("content_pieces.content_id"),
            nullable=False,
            index=True,
        ),
        sa.Column("platform", sa.String(50), nullable=False, index=True),
        sa.Column("recommended_time", sa.DateTime(), nullable=False, index=True),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="0"),
        sa.Column("reasoning", sa.String(2000), nullable=False, server_default=""),
        sa.Column("estimated_views", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
            index=True,
        ),
    )


def downgrade() -> None:
    op.drop_table("content_schedules")
    op.drop_table("content_campaigns")
    op.drop_table("content_pieces")
    op.drop_table("content_pipelines")
