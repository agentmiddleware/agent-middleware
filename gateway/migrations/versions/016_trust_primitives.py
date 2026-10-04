"""Trust primitives for permits, receipts, idempotency, and audit chains.

Revision ID: 016_trust_primitives
Revises: 015_policy_bundles
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "016_trust_primitives"
down_revision: Union[str, None] = "015_policy_bundles"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "signing_keys",
        sa.Column("key_id", sa.String(length=64), primary_key=True),
        sa.Column(
            "alg", sa.String(length=20), nullable=False, server_default="Ed25519"
        ),
        sa.Column("public_key_b64", sa.Text(), nullable=False),
        sa.Column(
            "status", sa.String(length=20), nullable=False, server_default="active"
        ),
        sa.Column(
            "created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("activated_at", sa.DateTime(), nullable=True),
        sa.Column("retired_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_signing_keys_status", "signing_keys", ["status"])
    op.create_index("ix_signing_keys_created_at", "signing_keys", ["created_at"])

    op.add_column(
        "control_plane_audit_events",
        sa.Column("payload_hash", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "control_plane_audit_events",
        sa.Column("previous_hash", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "control_plane_audit_events",
        sa.Column("chain_hash", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "control_plane_audit_events",
        sa.Column("signature", sa.Text(), nullable=True),
    )
    op.add_column(
        "control_plane_audit_events",
        sa.Column("signature_key_id", sa.String(length=64), nullable=True),
    )
    op.create_index(
        "ix_control_plane_audit_events_payload_hash",
        "control_plane_audit_events",
        ["payload_hash"],
    )
    op.create_index(
        "ix_control_plane_audit_events_chain_hash",
        "control_plane_audit_events",
        ["chain_hash"],
    )
    op.create_index(
        "ix_control_plane_audit_events_signature_key_id",
        "control_plane_audit_events",
        ["signature_key_id"],
    )

    op.create_table(
        "permits",
        sa.Column("permit_id", sa.String(length=64), primary_key=True),
        sa.Column("issuer_wallet_id", sa.String(length=50), nullable=False),
        sa.Column("subject_wallet_id", sa.String(length=50), nullable=False),
        sa.Column("subject_key_id", sa.String(length=50), nullable=True),
        sa.Column("scopes_json", sa.Text(), nullable=False),
        sa.Column("allowed_tools_json", sa.Text(), nullable=False),
        sa.Column("max_credits", sa.Numeric(precision=20, scale=8), nullable=False),
        sa.Column(
            "spent_credits",
            sa.Numeric(precision=20, scale=8),
            nullable=False,
            server_default="0",
        ),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("nonce", sa.String(length=64), nullable=False),
        sa.Column(
            "status", sa.String(length=20), nullable=False, server_default="active"
        ),
        sa.Column("signature", sa.Text(), nullable=False),
        sa.Column("key_id", sa.String(length=64), nullable=False),
        sa.Column(
            "issued_at", sa.DateTime(), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["issuer_wallet_id"], ["wallets.wallet_id"]),
        sa.ForeignKeyConstraint(["subject_wallet_id"], ["wallets.wallet_id"]),
        sa.ForeignKeyConstraint(["subject_key_id"], ["api_keys.key_id"]),
        sa.ForeignKeyConstraint(["key_id"], ["signing_keys.key_id"]),
        sa.UniqueConstraint("permit_id", "nonce", name="uq_permits_permit_nonce"),
    )
    op.create_index("ix_permits_issuer_wallet_id", "permits", ["issuer_wallet_id"])
    op.create_index("ix_permits_subject_wallet_id", "permits", ["subject_wallet_id"])
    op.create_index("ix_permits_subject_key_id", "permits", ["subject_key_id"])
    op.create_index("ix_permits_expires_at", "permits", ["expires_at"])
    op.create_index("ix_permits_nonce", "permits", ["nonce"])
    op.create_index("ix_permits_status", "permits", ["status"])
    op.create_index("ix_permits_key_id", "permits", ["key_id"])
    op.create_index("ix_permits_issued_at", "permits", ["issued_at"])

    op.create_table(
        "receipts",
        sa.Column("receipt_id", sa.String(length=64), primary_key=True),
        sa.Column("permit_id", sa.String(length=64), nullable=False),
        sa.Column("wallet_id", sa.String(length=50), nullable=False),
        sa.Column("key_id", sa.String(length=50), nullable=True),
        sa.Column("tool", sa.String(length=128), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("response_hash", sa.String(length=64), nullable=True),
        sa.Column("ledger_entry_id", sa.String(length=50), nullable=True),
        sa.Column(
            "credits_authorized", sa.Numeric(precision=20, scale=8), nullable=False
        ),
        sa.Column(
            "credits_charged",
            sa.Numeric(precision=20, scale=8),
            nullable=False,
            server_default="0",
        ),
        sa.Column("outcome", sa.String(length=32), nullable=False),
        sa.Column("audit_event_id", sa.String(length=50), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("signature", sa.Text(), nullable=False),
        sa.Column("signature_key_id", sa.String(length=64), nullable=False),
        sa.ForeignKeyConstraint(["permit_id"], ["permits.permit_id"]),
        sa.ForeignKeyConstraint(["wallet_id"], ["wallets.wallet_id"]),
        sa.ForeignKeyConstraint(["ledger_entry_id"], ["ledger_entries.entry_id"]),
        sa.ForeignKeyConstraint(
            ["audit_event_id"], ["control_plane_audit_events.event_id"]
        ),
        sa.ForeignKeyConstraint(["signature_key_id"], ["signing_keys.key_id"]),
    )
    op.create_index("ix_receipts_permit_id", "receipts", ["permit_id"])
    op.create_index("ix_receipts_wallet_id", "receipts", ["wallet_id"])
    op.create_index("ix_receipts_key_id", "receipts", ["key_id"])
    op.create_index("ix_receipts_tool", "receipts", ["tool"])
    op.create_index("ix_receipts_request_hash", "receipts", ["request_hash"])
    op.create_index("ix_receipts_ledger_entry_id", "receipts", ["ledger_entry_id"])
    op.create_index("ix_receipts_outcome", "receipts", ["outcome"])
    op.create_index("ix_receipts_audit_event_id", "receipts", ["audit_event_id"])
    op.create_index("ix_receipts_created_at", "receipts", ["created_at"])
    op.create_index("ix_receipts_signature_key_id", "receipts", ["signature_key_id"])

    op.create_table(
        "idempotency_records",
        sa.Column("record_id", sa.String(length=64), primary_key=True),
        sa.Column("wallet_id", sa.String(length=50), nullable=False),
        sa.Column("endpoint", sa.String(length=256), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("response_reference", sa.String(length=128), nullable=True),
        sa.Column("response_json", sa.Text(), nullable=True),
        sa.Column("status_code", sa.Integer(), nullable=False, server_default="200"),
        sa.Column(
            "created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["wallet_id"], ["wallets.wallet_id"]),
        sa.UniqueConstraint(
            "wallet_id",
            "endpoint",
            "idempotency_key",
            name="uq_idempotency_wallet_endpoint_key",
        ),
    )
    op.create_index(
        "ix_idempotency_records_wallet_id", "idempotency_records", ["wallet_id"]
    )
    op.create_index(
        "ix_idempotency_records_endpoint", "idempotency_records", ["endpoint"]
    )
    op.create_index(
        "ix_idempotency_records_idempotency_key",
        "idempotency_records",
        ["idempotency_key"],
    )
    op.create_index(
        "ix_idempotency_records_created_at", "idempotency_records", ["created_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_idempotency_records_created_at", table_name="idempotency_records")
    op.drop_index(
        "ix_idempotency_records_idempotency_key", table_name="idempotency_records"
    )
    op.drop_index("ix_idempotency_records_endpoint", table_name="idempotency_records")
    op.drop_index("ix_idempotency_records_wallet_id", table_name="idempotency_records")
    op.drop_table("idempotency_records")

    op.drop_index("ix_receipts_signature_key_id", table_name="receipts")
    op.drop_index("ix_receipts_created_at", table_name="receipts")
    op.drop_index("ix_receipts_audit_event_id", table_name="receipts")
    op.drop_index("ix_receipts_outcome", table_name="receipts")
    op.drop_index("ix_receipts_ledger_entry_id", table_name="receipts")
    op.drop_index("ix_receipts_request_hash", table_name="receipts")
    op.drop_index("ix_receipts_tool", table_name="receipts")
    op.drop_index("ix_receipts_key_id", table_name="receipts")
    op.drop_index("ix_receipts_wallet_id", table_name="receipts")
    op.drop_index("ix_receipts_permit_id", table_name="receipts")
    op.drop_table("receipts")

    op.drop_index("ix_permits_issued_at", table_name="permits")
    op.drop_index("ix_permits_key_id", table_name="permits")
    op.drop_index("ix_permits_status", table_name="permits")
    op.drop_index("ix_permits_nonce", table_name="permits")
    op.drop_index("ix_permits_expires_at", table_name="permits")
    op.drop_index("ix_permits_subject_key_id", table_name="permits")
    op.drop_index("ix_permits_subject_wallet_id", table_name="permits")
    op.drop_index("ix_permits_issuer_wallet_id", table_name="permits")
    op.drop_table("permits")

    op.drop_index(
        "ix_control_plane_audit_events_signature_key_id",
        table_name="control_plane_audit_events",
    )
    op.drop_index(
        "ix_control_plane_audit_events_chain_hash",
        table_name="control_plane_audit_events",
    )
    op.drop_index(
        "ix_control_plane_audit_events_payload_hash",
        table_name="control_plane_audit_events",
    )
    op.drop_column("control_plane_audit_events", "signature_key_id")
    op.drop_column("control_plane_audit_events", "signature")
    op.drop_column("control_plane_audit_events", "chain_hash")
    op.drop_column("control_plane_audit_events", "previous_hash")
    op.drop_column("control_plane_audit_events", "payload_hash")

    op.drop_index("ix_signing_keys_created_at", table_name="signing_keys")
    op.drop_index("ix_signing_keys_status", table_name="signing_keys")
    op.drop_table("signing_keys")
