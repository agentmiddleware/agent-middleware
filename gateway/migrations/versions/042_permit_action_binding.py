"""Add nullable signed single-action binding fields.

Revision ID: 042_permit_action_binding
Revises: 041_scrub_content_owner_keys
"""

from alembic import op
import sqlalchemy as sa

revision: str = "042_permit_action_binding"
down_revision: str = "041_scrub_content_owner_keys"
branch_labels = None
depends_on = None

_FIELDS = (
    "action_contract_version",
    "action_payload_hash",
    "action_schema_id",
    "action_schema_version",
    "action_public_tool_id",
    "action_upstream_binding_hash",
)


def upgrade():
    for table in ("permits", "receipts"):
        with op.batch_alter_table(table, schema=None) as batch_op:
            for name in _FIELDS:
                batch_op.add_column(
                    sa.Column(
                        name,
                        sa.Integer()
                        if name == "action_contract_version"
                        else sa.String(),
                        nullable=True,
                    )
                )


def downgrade():
    # Rollback disables admission while capable workers retain reconciliation.
    # Even partial bindings/unknown versions and orphan tombstones are authority
    # we cannot safely reinterpret as legacy envelopes. Never discard them.
    connection = op.get_bind()
    if connection.dialect.name == "postgresql":
        connection.execute(
            sa.text(
                "LOCK TABLE permits, receipts, idempotency_records IN ACCESS EXCLUSIVE MODE"
            )
        )
    elif connection.dialect.name == "sqlite":
        connection.execute(sa.text("BEGIN IMMEDIATE"))
    for table in ("permits", "receipts"):
        predicate = " OR ".join(f"{name} IS NOT NULL" for name in _FIELDS)
        if connection.execute(
            sa.text(f"SELECT 1 FROM {table} WHERE {predicate} LIMIT 1")
        ).first():
            raise RuntimeError(
                "action_authority_retained: disable admission and retain schema"
            )
    if connection.execute(
        sa.text(
            "SELECT 1 FROM idempotency_records WHERE endpoint = '/mcp/action/v1' LIMIT 1"
        )
    ).first():
        raise RuntimeError("action_authority_retained: retain action owner tombstones")
    for table in ("receipts", "permits"):
        with op.batch_alter_table(table, schema=None) as batch_op:
            for name in reversed(_FIELDS):
                batch_op.drop_column(name)
