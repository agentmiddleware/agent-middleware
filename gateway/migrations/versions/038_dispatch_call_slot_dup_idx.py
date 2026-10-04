"""add dispatch call slot and duplicate detection index

Revision ID: 038
Revises: 037
Create Date: 2026-09-27

"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "038_dispatch_call_slot_dup_idx"
down_revision: str = "037_mcp_dispatch_claim_hash"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add call_slot_reserved column and duplicate detection index.

    The call_slot_reserved column tracks whether a dispatch attempt holds a
    per-tool call slot from the permit's max_calls_per_tool cap. The slot is
    reserved atomically with the budget at preparation time and released only
    when the attempt is compensated before dispatch.

    The (permit_id, public_tool_id, request_hash, created_at) index enables
    cross-key repeat detection: when a new idempotency key arrives with the
    same request hash as an active or succeeded attempt under the same permit
    and tool, it can be detected and refused as a duplicate.
    """
    connection = op.get_bind()
    # Entering autocommit_block commits this column before the revision stamp.
    # A failed concurrent index build therefore leaves a resumable partial
    # migration. Skip only the exact column shape this revision installs.
    existing_column = None
    if connection.dialect.name == "postgresql":
        existing_column = next(
            (
                column
                for column in sa.inspect(connection).get_columns(
                    "mcp_dispatch_attempts"
                )
                if column["name"] == "call_slot_reserved"
            ),
            None,
        )
    if existing_column is not None:
        if (
            not isinstance(existing_column["type"], sa.Boolean)
            or existing_column["nullable"]
            or existing_column["default"]
            not in ("false", "'0'::boolean", "'false'::boolean")
        ):
            raise RuntimeError("migration038_incompatible_call_slot_reserved")
    else:
        op.add_column(
            "mcp_dispatch_attempts",
            sa.Column(
                "call_slot_reserved", sa.Boolean(), nullable=False, server_default="0"
            ),
        )

    # Create index for duplicate detection.
    # Use CONCURRENTLY on Postgres to avoid blocking writes during index build.
    # SQLite doesn't support CONCURRENTLY, so check the connection type.
    if connection.dialect.name == "postgresql":
        # Postgres: use CONCURRENTLY in an autocommit block with if-not-exists guard
        with op.get_context().autocommit_block():
            # Check if index exists and whether it's valid
            # An INVALID index (from a failed CONCURRENT build) should be dropped and rebuilt
            result = connection.execute(
                sa.text(
                    "SELECT indisvalid FROM pg_index i "
                    "JOIN pg_class c ON i.indexrelid = c.oid "
                    "WHERE c.relname = 'ix_mcp_dispatch_attempts_duplicate_detection'"
                )
            ).fetchone()

            if result is not None:
                # Index exists - check if it's valid
                if not result[0]:  # indisvalid is False
                    # Drop the invalid index and recreate it
                    connection.execute(
                        sa.text(
                            "DROP INDEX CONCURRENTLY IF EXISTS ix_mcp_dispatch_attempts_duplicate_detection"
                        )
                    )
                    connection.execute(
                        sa.text(
                            "CREATE INDEX CONCURRENTLY ix_mcp_dispatch_attempts_duplicate_detection "
                            "ON mcp_dispatch_attempts (permit_id, public_tool_id, request_hash, created_at)"
                        )
                    )
                # else: valid index already exists, nothing to do
            else:
                # Index doesn't exist, create it
                connection.execute(
                    sa.text(
                        "CREATE INDEX CONCURRENTLY ix_mcp_dispatch_attempts_duplicate_detection "
                        "ON mcp_dispatch_attempts (permit_id, public_tool_id, request_hash, created_at)"
                    )
                )
    else:
        # SQLite: regular index creation
        op.create_index(
            "ix_mcp_dispatch_attempts_duplicate_detection",
            "mcp_dispatch_attempts",
            ["permit_id", "public_tool_id", "request_hash", "created_at"],
            unique=False,
        )


def downgrade() -> None:
    """Remove call_slot_reserved column and duplicate detection index."""
    op.drop_index(
        "ix_mcp_dispatch_attempts_duplicate_detection",
        table_name="mcp_dispatch_attempts",
    )

    with op.batch_alter_table("mcp_dispatch_attempts", schema=None) as batch_op:
        batch_op.drop_column("call_slot_reserved")
