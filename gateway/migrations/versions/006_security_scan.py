"""Add security_scans + security_vulnerabilities tables

Revision ID: 006_security_scan
Revises: 005_oracle_tables
Create Date: 2026-04-17

Consolidates red_team.ScanStore and rtaas._jobs into a shared schema
discriminated on ``scan_type``. See issue #30.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "006_security_scan"
down_revision: Union[str, None] = "005_oracle_tables"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "security_scans",
        sa.Column("scan_id", sa.String(64), primary_key=True),
        sa.Column("scan_type", sa.String(20), nullable=False, index=True),
        sa.Column("tenant_id", sa.String(100), nullable=True, index=True),
        sa.Column("targets_json", sa.Text(), nullable=True),
        sa.Column("attack_categories_json", sa.Text(), nullable=True),
        sa.Column(
            "intensity", sa.String(20), nullable=False, server_default="standard"
        ),
        sa.Column("status", sa.String(20), nullable=False, index=True),
        sa.Column("total_tests_run", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_passed", sa.Integer(), nullable=True),
        sa.Column("total_failed", sa.Integer(), nullable=True),
        sa.Column("security_score", sa.Float(), nullable=False, server_default="0"),
        sa.Column("recommendations_json", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
            index=True,
        ),
    )

    op.create_table(
        "security_vulnerabilities",
        sa.Column("vuln_id", sa.String(64), primary_key=True),
        sa.Column(
            "scan_id",
            sa.String(64),
            sa.ForeignKey("security_scans.scan_id"),
            nullable=False,
            index=True,
        ),
        sa.Column("category", sa.String(50), nullable=False, index=True),
        sa.Column("severity", sa.String(20), nullable=False, index=True),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("description", sa.String(4000), nullable=False, server_default=""),
        sa.Column("endpoint", sa.String(2048), nullable=False),
        sa.Column("method", sa.String(10), nullable=True),
        sa.Column("evidence_json", sa.Text(), nullable=True),
        sa.Column("remediation", sa.String(4000), nullable=False, server_default=""),
        sa.Column(
            "remediation_status",
            sa.String(30),
            nullable=False,
            server_default="open",
        ),
        sa.Column("cwe_id", sa.String(20), nullable=True),
        sa.Column(
            "discovered_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
            index=True,
        ),
    )


def downgrade() -> None:
    op.drop_table("security_vulnerabilities")
    op.drop_table("security_scans")
