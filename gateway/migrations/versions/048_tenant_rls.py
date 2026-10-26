"""Wallet row-level security policies; enabled only under TENANT_RLS_MODE=enforce.

Revision ID: 048_tenant_rls
Revises: 047_dispatch_reconcile_lease

PostgreSQL only; a no-op on SQLite. Each listed table gets one policy whose USING and WITH CHECK admit a row only
when an owning wallet column equals ``app.current_wallet_id`` or
the app.rls_bypass setting is ``'on'``. ``current_setting(..., true)`` yields NULL when
the setting was never set and '' after a ``SET LOCAL`` ends; NULLIF folds both
into NULL, and a NULL comparison admits nothing, so an unset scope fails
closed.

Default off: the policies are created but row-level security stays disabled
on every table, so no client sees any change. ENABLE and FORCE ROW LEVEL
SECURITY run only when this migration executes with TENANT_RLS_MODE=enforce;
enabling later is the per-table ALTER in docs/tenant-rls.md in the api repo. Wallet columns are character varying; the setting is text, compared with
the built-in text equality, so no cast is needed and the wallet indexes stay
usable.

Superusers and BYPASSRLS roles skip every policy. FORCE binds the table owner
too, so this migration (and any later data migration) runs with
the app.rls_bypass setting set to 'on' for the migration transaction only by
migrations/env.py (``set_config(..., true)``). Runtime role setup and
the admin mechanism are documented in docs/tenant-rls.md in the api repo.

A second, SELECT-only policy mirrors the delegated-issuer branch of
``authorize_receipt_access``: the issuer of a permit may read receipts filed
under that permit's subject wallet, and the ledger entry (and its refund), the
dispatch attempt, and the idempotency record such a visible receipt points at.
Those subqueries are themselves filtered by the receipts and permits policies,
so they can only widen reads along links the caller can already see. Writes
stay limited to the caller's own wallet.

The table list is a frozen copy of app.db.tenant_rls.RLS_TABLES in the api repo.

Ported from the archived agent-middleware-api chain with the same revision
id. ``Settings.TENANT_RLS_MODE`` is ported with the same name, default
(``off``) and validation, so this migration behaves as the original did.
migrations/env.py sets the app.rls_bypass setting exactly as the archived
env.py did, so data migrations still see every row under FORCE.

This repository's chain also has the wallet-scoped receipt_resolutions and
resolution_chain_heads (044), which the api chain never had. They get the
same tenant policy here when they exist (a fresh chain, where 044 ran
first). On a database that reached 050 through the api chain they do not
exist yet; 053_repair_api_lineage creates them and applies the identical
policy, so both lineages end with the same policy set.
"""

import sqlalchemy as sa
from alembic import op

from app.core.config import get_settings

revision: str = "048_tenant_rls"
down_revision: str = "047_dispatch_reconcile_lease"
branch_labels = None
depends_on = None

POLICY_NAME = "amw_tenant_isolation"

RLS_TABLES: dict[str, tuple[str, ...]] = {
    "api_keys": ("wallet_id",),
    "billing_alerts": ("wallet_id",),
    "daily_balance_snapshots": ("wallet_id",),
    "human_approvals": ("wallet_id",),
    "idempotency_records": ("wallet_id",),
    "key_rotation_logs": ("wallet_id",),
    "kyc_verifications": ("wallet_id",),
    "ledger_entries": ("wallet_id",),
    "mcp_dispatch_attempts": ("wallet_id",),
    "permit_requests": ("issuer_wallet_id", "subject_wallet_id"),
    "permits": ("issuer_wallet_id", "subject_wallet_id"),
    "policy_bundles": ("wallet_id",),
    "quotes": ("wallet_id",),
    "receipts": ("wallet_id",),
    "refresh_tokens": ("wallet_id",),
}

# Tables from this repo's 044 that are absent on an api-chain database when
# 048 runs there; 053_repair_api_lineage applies the same policy to them.
LATE_RLS_TABLES: dict[str, tuple[str, ...]] = {
    "receipt_resolutions": ("wallet_id",),
    "resolution_chain_heads": ("wallet_id",),
}

_CURRENT_WALLET = "NULLIF(current_setting('app.current_wallet_id', true), '')"
_BYPASS = "current_setting('app.rls_bypass', true) = 'on'"

READ_POLICY_NAME = "amw_delegated_issuer_read"

DELEGATED_READ_PREDICATES: dict[str, str] = {
    "receipts": (
        "EXISTS (SELECT 1 FROM permits p WHERE p.permit_id = receipts.permit_id "
        "AND p.subject_wallet_id = receipts.wallet_id "
        f"AND p.issuer_wallet_id = {_CURRENT_WALLET})"
    ),
    "ledger_entries": (
        "EXISTS (SELECT 1 FROM receipts r WHERE r.ledger_entry_id = "
        "ledger_entries.entry_id OR r.ledger_entry_id = ledger_entries.correlation_id)"
    ),
    "mcp_dispatch_attempts": (
        "EXISTS (SELECT 1 FROM receipts r "
        "WHERE r.dispatch_attempt_id = mcp_dispatch_attempts.attempt_id)"
    ),
    "idempotency_records": (
        "EXISTS (SELECT 1 FROM receipts r "
        "WHERE r.idempotency_record_id = idempotency_records.record_id)"
    ),
}


def policy_predicate(columns: tuple[str, ...]) -> str:
    wallet_match = " OR ".join(f"{column} = {_CURRENT_WALLET}" for column in columns)
    return f"({wallet_match}) OR {_BYPASS}"


def _apply_tenant_policy(table: str, columns: tuple[str, ...], enforce: bool):
    predicate = policy_predicate(columns)
    if enforce:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(f"DROP POLICY IF EXISTS {POLICY_NAME} ON {table}")
    op.execute(
        f"CREATE POLICY {POLICY_NAME} ON {table} AS PERMISSIVE FOR ALL "
        f"USING ({predicate}) WITH CHECK ({predicate})"
    )


def upgrade():
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    enforce = get_settings().TENANT_RLS_MODE == "enforce"
    for table, columns in RLS_TABLES.items():
        _apply_tenant_policy(table, columns, enforce)
    existing = set(sa.inspect(bind).get_table_names())
    for table, columns in LATE_RLS_TABLES.items():
        if table in existing:
            _apply_tenant_policy(table, columns, enforce)
    for table, predicate in DELEGATED_READ_PREDICATES.items():
        op.execute(f"DROP POLICY IF EXISTS {READ_POLICY_NAME} ON {table}")
        op.execute(
            f"CREATE POLICY {READ_POLICY_NAME} ON {table} AS PERMISSIVE FOR SELECT "
            f"USING ({predicate})"
        )


def downgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    for table in DELEGATED_READ_PREDICATES:
        op.execute(f"DROP POLICY IF EXISTS {READ_POLICY_NAME} ON {table}")
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    late = [table for table in LATE_RLS_TABLES if table in existing]
    for table in [*RLS_TABLES, *late]:
        op.execute(f"DROP POLICY IF EXISTS {POLICY_NAME} ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
