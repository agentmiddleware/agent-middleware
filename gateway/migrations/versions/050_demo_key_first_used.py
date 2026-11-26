"""Record the first authenticated use of an API key; demo funnel view.

Revision ID: 050_demo_key_first_used
Revises: 049_demo_key_attribution

Additive only. ``api_keys.first_used_at`` is nullable and is backfilled from
``last_used_at`` where a key has already been used (an UPDATE of the new
column only; no row is deleted and no other column changes). The read-only
view ``demo_key_funnel_by_source`` groups demo-tenant keys (``tenant =
'demo'``, the label ``app.services.demo_tenant`` stamps on issuance) by
acquisition source and counts issued versus used keys.

PostgreSQL groups by the referrer's host. Other dialects (SQLite in tests)
group by the raw referrer, because they have no portable host extraction.
Ported from the archived agent-middleware-api chain with the same revision
id. In this repo ``043_demo_key_fields`` already creates the column and the
view, so the upgrade adds the column and view only when missing, the
backfill only touches NULL ``first_used_at``, and the downgrade leaves both
to 043.
"""

from alembic import op
import sqlalchemy as sa

revision: str = "050_demo_key_first_used"
down_revision: str = "049_demo_key_attribution"
branch_labels = None
depends_on = None

VIEW_NAME = "demo_key_funnel_by_source"

# First capture group = host of an absolute URL, skipping any userinfo.
_PG_REFERRER_HOST = (
    "COALESCE(lower(substring(referrer from "
    "'^[A-Za-z][A-Za-z0-9+.-]*://(?:[^@/?#]*@)?([^/:?#]+)')), referrer)"
)


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


def upgrade():
    bind = op.get_bind()
    existing = {c["name"] for c in sa.inspect(bind).get_columns("api_keys")}
    if "first_used_at" not in existing:
        with op.batch_alter_table("api_keys", schema=None) as batch_op:
            batch_op.add_column(
                sa.Column("first_used_at", sa.DateTime(), nullable=True)
            )

    op.execute(
        "UPDATE api_keys SET first_used_at = last_used_at "
        "WHERE first_used_at IS NULL AND last_used_at IS NOT NULL"
    )

    if VIEW_NAME in sa.inspect(bind).get_view_names():
        return
    if bind.dialect.name == "postgresql":
        op.execute(_view_sql(_PG_REFERRER_HOST))
    else:
        op.execute(_view_sql("referrer"))


def downgrade():
    """Column and view belong to 043_demo_key_fields, which retains them."""
