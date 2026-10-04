"""Force the background recovery pass on the live quickstart DB and report what
it does with the crash-orphaned records: repaired (a receipt existed for the
charged ledger entry -> record completed) vs needs_review (charged, no receipt ->
flagged for a human, never auto-redispatched). Proves the local (SQLite) instance
runs the same reconciliation the Postgres boundary proof asserts — no silent loss,
no double charge.
"""

import asyncio
import os
import sys

ROOT = os.environ.get("TP_ROOT", os.getcwd())
STATE = os.environ.get("TP_STATE_DIR", os.path.join(ROOT, "data", "quickstart"))
os.environ.update(
    {
        "PYTHONPATH": ROOT,
        "ENVIRONMENT": "local",
        "DEBUG": "false",
        "DATABASE_URL": f"sqlite+aiosqlite:///{STATE}/api.db",
        "STATE_BACKEND": "sqlite",
        "SQLITE_URL": f"{STATE}/state.db",
        "VALID_API_KEYS": "",
        "STATIC_DEV_API_KEYS": "",
        "TRUST_MODE_ENABLED": "true",
        "ENABLE_PROOF_SURFACES": "false",
        "ENABLE_DOGFOOD_TOOL": "true",
        "ENABLE_DEV_KEY_SELF_PROVISION": "true",
        "MCP_UPSTREAM_ENABLED": "false",
        "TRUST_SIGNING_KEY_ID": "quickstart-local-ed25519",
        "TRUST_SIGNING_PRIVATE_KEY_B64": open(f"{STATE}/signing-seed.b64")
        .read()
        .strip(),
    }
)
sys.path.insert(0, ROOT)


async def main():
    from app.services.idempotency import get_idempotency_service

    # idle_seconds=0 -> reconcile every stuck record now, regardless of age
    repaired, needs_review = await get_idempotency_service().reconcile_stuck_records(
        idle_seconds=0
    )
    print(
        f"reconcile_stuck_records(idle_seconds=0): repaired={repaired}, needs_review={needs_review}"
    )


if __name__ == "__main__":
    asyncio.run(main())
