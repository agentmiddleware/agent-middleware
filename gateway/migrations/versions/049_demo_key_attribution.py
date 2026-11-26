"""Store optional UTM and referrer attribution on demo API keys.

Revision ID: 049_demo_key_attribution
Revises: 048_tenant_rls

Nullable columns. Existing keys, including demo keys issued before this
migration, stay NULL. Only POST /v1/demo/keys writes them.

Ported from the archived agent-middleware-api chain with the same revision
id. In this repo ``043_demo_key_fields`` already adds these columns, so the
upgrade only adds any that are missing and the downgrade leaves them to 043.
"""

from alembic import op
import sqlalchemy as sa

revision: str = "049_demo_key_attribution"
down_revision: str = "048_tenant_rls"
branch_labels = None
depends_on = None


COLUMNS = (
    ("utm_source", 128),
    ("utm_medium", 128),
    ("utm_campaign", 128),
    ("utm_content", 128),
    ("utm_term", 128),
    ("referrer", 512),
)


def upgrade():
    existing = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("api_keys")}
    missing = [(name, size) for name, size in COLUMNS if name not in existing]
    if not missing:
        return
    with op.batch_alter_table("api_keys", schema=None) as batch_op:
        for name, size in missing:
            batch_op.add_column(sa.Column(name, sa.String(length=size), nullable=True))


def downgrade():
    """Columns belong to 043_demo_key_fields, which retains them on rollback."""
