"""Harden the human-approval gate: bind approvals to the request, repair
the SQLite boolean backfill from 023.

Two changes:

1. ``human_approvals.request_hash`` — sha256 of the
   ``(tool, arguments, estimated_credits)`` request the human reviewed. Lets a
   reloaded approval reject an invoke that reused the same idempotency key with
   different arguments or a different current price.

2. Repair text booleans on SQLite. Migrations 022 and 023 added
   ``refresh_tokens.revoked``, ``permits.requires_human_approval``, and
   ``human_approvals.simulated`` with the string server default ``"false"``.
   SQLite stores that as the text ``'false'``. SQLAlchemy then reads any
   non-empty text as true, so a live refresh token looks revoked, a permit
   looks like it requires human approval (and fails signature verification,
   because the flag enters the signed payload only when true), and a real
   approval looks simulated. Postgres parses ``'false'`` as boolean false, so
   it is unaffected. The repair runs on SQLite only and rewrites the known
   text values in the correct direction. Unexpected text is left alone.

Revision ID: 024_human_approval_hardening
Revises: 023_human_approval_gate
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "024_human_approval_hardening"
down_revision: Union[str, None] = "023_human_approval_gate"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "human_approvals",
        sa.Column("request_hash", sa.String(length=64), nullable=True),
    )

    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        # Rows created under the old string default hold the text 'false'.
        # Repair only the known text booleans, in the correct direction, so an
        # unexpected value is never silently turned into "not set". The old
        # server default can only produce 'false', but the 'true' side is
        # handled so a real true is not left as text.
        for table, column in (
            ("permits", "requires_human_approval"),
            ("human_approvals", "simulated"),
            ("refresh_tokens", "revoked"),
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


def downgrade() -> None:
    op.drop_column("human_approvals", "request_hash")
