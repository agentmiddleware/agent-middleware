"""Serialize legacy and canonical governed MCP idempotency identities.

Revision ID: 027_governed_mcp_identity
Revises: 026_governed_mcp_persistence
Create Date: 2026-08-07
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "027_governed_mcp_identity"
down_revision: Union[str, None] = "026_governed_mcp_persistence"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_INDEX_NAME = "uq_idempotency_governed_mcp_identity"
_GOVERNED_ENDPOINT_PREDICATE = """
endpoint = '/mcp/invoke'
OR endpoint = '/mcp/messages'
OR endpoint LIKE '/mcp/tools/%/invoke'
"""


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        # INSERT takes ROW EXCLUSIVE, so this prevents an old worker from
        # creating a second endpoint identity between the count-only preflight
        # and index installation. The lock lasts for this migration transaction.
        op.execute(
            sa.text("LOCK TABLE idempotency_records IN SHARE ROW EXCLUSIVE MODE")
        )
    ambiguous_groups = bind.scalar(
        sa.text(
            f"""
            SELECT COUNT(*)
            FROM (
                SELECT wallet_id, idempotency_key
                FROM idempotency_records
                WHERE {_GOVERNED_ENDPOINT_PREDICATE}
                GROUP BY wallet_id, idempotency_key
                HAVING COUNT(*) > 1
            ) AS ambiguous_governed_mcp_identities
            """
        )
    )
    if int(ambiguous_groups or 0):
        # Do not print wallet ids or caller-supplied keys, and do not guess
        # which historical endpoint owns an ambiguous action. An operator must
        # adjudicate the rows before this fail-closed migration can continue.
        raise RuntimeError(
            "cannot install governed MCP identity invariant: "
            f"{int(ambiguous_groups)} ambiguous wallet/key group(s)"
        )

    # This expression index is intentionally compatible with pre-026 binaries.
    # Install the migration before rolling out current workers: an old worker
    # inserting /mcp/messages and a current worker inserting /mcp/invoke then
    # contend on the same database key, so only one pipeline can proceed.
    op.execute(
        sa.text(
            f"""
            CREATE UNIQUE INDEX {_INDEX_NAME}
            ON idempotency_records (
                wallet_id,
                (
                    CASE
                        WHEN {_GOVERNED_ENDPOINT_PREDICATE}
                        THEN '/mcp/invoke'
                        ELSE endpoint
                    END
                ),
                idempotency_key
            )
            """
        )
    )


def downgrade() -> None:
    # A code rollback should normally retain this additive, old-binary-safe
    # invariant. Dropping it after canonical rows exist reopens the possibility
    # that an old worker creates a second legacy identity for the same action.
    op.drop_index(_INDEX_NAME, table_name="idempotency_records")
