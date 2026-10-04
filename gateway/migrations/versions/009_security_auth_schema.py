"""Add API key and wallet auth schema

Revision ID: 009_security_auth_schema
Revises: 008_iot_devices
Create Date: 2026-05-01

Aligns production migrations with the SQLModel tables used by wallet-scoped
API-key authentication, key rotation, KYC verification, and service registry.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "009_security_auth_schema"
down_revision: Union[str, None] = "008_iot_devices"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("wallets", sa.Column("child_agent_id", sa.String(100), nullable=True))
    op.add_column(
        "wallets",
        sa.Column("max_spend", sa.Numeric(precision=20, scale=8), nullable=True),
    )
    op.add_column(
        "wallets", sa.Column("task_description", sa.String(500), nullable=True)
    )
    op.add_column("wallets", sa.Column("ttl_seconds", sa.Integer(), nullable=True))
    op.add_column(
        "wallets",
        sa.Column(
            "kyc_status",
            sa.String(30),
            nullable=False,
            server_default="not_required",
        ),
    )
    op.add_column("wallets", sa.Column("kyc_verified_at", sa.DateTime(), nullable=True))

    op.create_table(
        "service_registry",
        sa.Column("service_id", sa.String(100), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.String(1000), nullable=False, server_default=""),
        sa.Column(
            "owner_wallet_id",
            sa.String(50),
            sa.ForeignKey("wallets.wallet_id"),
            nullable=False,
        ),
        sa.Column("owner_key", sa.String(255), nullable=False),
        sa.Column("category", sa.String(50), nullable=False),
        sa.Column(
            "credits_per_unit", sa.Numeric(precision=20, scale=8), nullable=False
        ),
        sa.Column("unit_name", sa.String(50), nullable=False, server_default="request"),
        sa.Column("mcp_manifest", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("metadata_json", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_service_registry_owner_wallet_id",
        "service_registry",
        ["owner_wallet_id"],
    )
    op.create_index("ix_service_registry_owner_key", "service_registry", ["owner_key"])
    op.create_index("ix_service_registry_category", "service_registry", ["category"])

    op.create_table(
        "kyc_verifications",
        sa.Column("verification_id", sa.String(100), primary_key=True),
        sa.Column(
            "wallet_id",
            sa.String(50),
            sa.ForeignKey("wallets.wallet_id"),
            nullable=False,
        ),
        sa.Column("stripe_session_id", sa.String(100), nullable=False, unique=True),
        sa.Column("status", sa.String(30), nullable=False, server_default="pending"),
        sa.Column(
            "verification_type",
            sa.String(20),
            nullable=False,
            server_default="identity",
        ),
        sa.Column("document_type", sa.String(30), nullable=True),
        sa.Column("first_verified_at", sa.DateTime(), nullable=True),
        sa.Column("last_verified_at", sa.DateTime(), nullable=True),
        sa.Column("rejection_reason", sa.String(500), nullable=True),
        sa.Column("metadata_json", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_kyc_verifications_wallet_id", "kyc_verifications", ["wallet_id"]
    )
    op.create_index(
        "ix_kyc_verifications_stripe_session_id",
        "kyc_verifications",
        ["stripe_session_id"],
        unique=True,
    )
    op.create_index(
        "ix_kyc_verifications_created_at", "kyc_verifications", ["created_at"]
    )

    op.create_table(
        "api_keys",
        sa.Column("key_id", sa.String(50), primary_key=True),
        sa.Column(
            "wallet_id",
            sa.String(50),
            sa.ForeignKey("wallets.wallet_id"),
            nullable=False,
        ),
        sa.Column("key_hash", sa.String(64), nullable=False),
        sa.Column("key_prefix", sa.String(8), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("rotation_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_rotated_at", sa.DateTime(), nullable=True),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
        sa.Column("last_used_ip", sa.String(45), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column("revoke_reason", sa.String(255), nullable=True),
        sa.Column("metadata_json", sa.Text(), nullable=True),
    )
    op.create_index("ix_api_keys_wallet_id", "api_keys", ["wallet_id"])
    op.create_index("ix_api_keys_key_hash", "api_keys", ["key_hash"])
    op.create_index("ix_api_keys_created_at", "api_keys", ["created_at"])

    op.create_table(
        "key_rotation_logs",
        sa.Column("log_id", sa.String(50), primary_key=True),
        sa.Column("key_id", sa.String(50), nullable=False),
        sa.Column(
            "wallet_id",
            sa.String(50),
            sa.ForeignKey("wallets.wallet_id"),
            nullable=False,
        ),
        sa.Column("rotation_type", sa.String(30), nullable=False),
        sa.Column("old_key_id", sa.String(50), nullable=True),
        sa.Column("new_key_id", sa.String(50), nullable=True),
        sa.Column("trigger_reason", sa.String(255), nullable=False),
        sa.Column("triggered_by", sa.String(50), nullable=False),
        sa.Column("ip_address", sa.String(45), nullable=True),
        sa.Column("user_agent", sa.String(500), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_key_rotation_logs_key_id", "key_rotation_logs", ["key_id"])
    op.create_index(
        "ix_key_rotation_logs_wallet_id", "key_rotation_logs", ["wallet_id"]
    )
    op.create_index(
        "ix_key_rotation_logs_created_at", "key_rotation_logs", ["created_at"]
    )


def downgrade() -> None:
    op.drop_table("key_rotation_logs")
    op.drop_table("api_keys")
    op.drop_table("kyc_verifications")
    op.drop_table("service_registry")

    op.drop_column("wallets", "kyc_verified_at")
    op.drop_column("wallets", "kyc_status")
    op.drop_column("wallets", "ttl_seconds")
    op.drop_column("wallets", "task_description")
    op.drop_column("wallets", "max_spend")
    op.drop_column("wallets", "child_agent_id")
