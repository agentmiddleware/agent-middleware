"""Add demo tenant labels, key attribution, and first-use funnel data.

Revision ID: 043_demo_key_fields
Revises: 042_permit_action_binding

Every column is nullable so existing wallets and keys remain normal. Re-running
the upgrade only fills missing schema pieces and unfilled first-use timestamps.
"""

from alembic import op
import sqlalchemy as sa

revision: str = "043_demo_key_fields"
down_revision: str = "042_permit_action_binding"
branch_labels = None
depends_on = None

VIEW_NAME = "demo_key_funnel_by_source"
_PG_REFERRER_HOST = (
    "COALESCE(lower(substring(referrer from "
    "'^[A-Za-z][A-Za-z0-9+.-]*://(?:[^@/?#]*@)?([^/:?#]+)')), referrer)"
)


def _add_missing_columns(table: str, columns: list[sa.Column]) -> None:
    existing = {
        column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)
    }
    missing = [column for column in columns if column.name not in existing]
    if missing:
        with op.batch_alter_table(table) as batch_op:
            for column in missing:
                batch_op.add_column(column)


def _create_tenant_index(table: str) -> None:
    name = f"ix_{table}_tenant"
    existing = {index["name"] for index in sa.inspect(op.get_bind()).get_indexes(table)}
    if name not in existing:
        op.create_index(name, table, ["tenant"])


def _view_sql(referrer_expr: str) -> str:
    return f"""
        CREATE VIEW {VIEW_NAME} AS
        SELECT
            COALESCE(utm_source, '(none)') AS utm_source,
            utm_medium,
            utm_campaign,
            {referrer_expr} AS referrer_host,
            COUNT(*) AS keys_issued,
            COUNT(first_used_at) AS keys_used,
            MIN(created_at) AS first_issued_at,
            MAX(created_at) AS last_issued_at
        FROM api_keys
        WHERE tenant = 'demo'
        GROUP BY
            COALESCE(utm_source, '(none)'),
            utm_medium,
            utm_campaign,
            {referrer_expr}
    """


def upgrade() -> None:
    _add_missing_columns("wallets", [sa.Column("tenant", sa.String(32), nullable=True)])
    _add_missing_columns(
        "api_keys",
        [
            sa.Column("tenant", sa.String(32), nullable=True),
            sa.Column("allowed_tools_json", sa.Text(), nullable=True),
            sa.Column("utm_source", sa.String(128), nullable=True),
            sa.Column("utm_medium", sa.String(128), nullable=True),
            sa.Column("utm_campaign", sa.String(128), nullable=True),
            sa.Column("utm_content", sa.String(128), nullable=True),
            sa.Column("utm_term", sa.String(128), nullable=True),
            sa.Column("referrer", sa.String(512), nullable=True),
            sa.Column("first_used_at", sa.DateTime(), nullable=True),
        ],
    )
    _create_tenant_index("wallets")
    _create_tenant_index("api_keys")
    op.execute(
        sa.text(
            "UPDATE api_keys SET first_used_at = last_used_at "
            "WHERE first_used_at IS NULL AND last_used_at IS NOT NULL"
        )
    )
    referrer_expr = (
        _PG_REFERRER_HOST if op.get_bind().dialect.name == "postgresql" else "referrer"
    )
    if VIEW_NAME not in sa.inspect(op.get_bind()).get_view_names():
        op.execute(_view_sql(referrer_expr))


def downgrade() -> None:
    """Retain demo labels and attribution so rollback cannot erase evidence."""
