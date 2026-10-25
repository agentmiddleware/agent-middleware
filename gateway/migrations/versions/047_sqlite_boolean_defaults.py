"""Repair legacy SQLite text booleans and replace their text defaults.

Revision ID: 047_sqlite_boolean_defaults
Revises: 046_api_key_usage_events
"""

from alembic import op
import sqlalchemy as sa

revision: str = "047_sqlite_boolean_defaults"
down_revision: str = "046_api_key_usage_events"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "sqlite":
        return

    for table, column in (
        ("refresh_tokens", "revoked"),
        ("human_approvals", "simulated"),
        ("permits", "requires_human_approval"),
    ):
        op.execute(
            sa.text(
                f"UPDATE {table} SET {column} = 0 "
                f"WHERE typeof({column}) = 'text' "
                f"AND lower({column}) IN ('false', 'f', '0', 'no')"
            )
        )
        op.execute(
            sa.text(
                f"UPDATE {table} SET {column} = 1 "
                f"WHERE typeof({column}) = 'text' "
                f"AND lower({column}) IN ('true', 't', '1', 'yes')"
            )
        )
        with op.batch_alter_table(table) as batch_op:
            batch_op.alter_column(
                column,
                existing_type=sa.Boolean(),
                existing_nullable=False,
                server_default=sa.false(),
            )


def downgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        raise RuntimeError("sqlite_boolean_defaults_downgrade_unsupported")
