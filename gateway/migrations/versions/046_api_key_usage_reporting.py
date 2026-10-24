"""Register the additional API key usage reporting route.

Revision ID: 046_api_key_usage_reporting
Revises: 045_api_key_usage_events

PR #841 already created the event table and daily view. PR #840's schema
objects overlap exactly, so this revision makes no database changes.
"""

revision: str = "046_api_key_usage_reporting"
down_revision: str = "045_api_key_usage_events"
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
