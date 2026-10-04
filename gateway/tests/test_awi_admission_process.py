"""Process death preserves AWI debit ownership without redispatch."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.anyio
@pytest.mark.parametrize(
    "crash", ["before_callback", "after_callback", "after_receipt"]
)
async def test_awi_process_crash_holds_or_recovers_owner(
    tmp_path, crash, clean_database
):
    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url.startswith("postgresql+"):
        database_url = f"sqlite+aiosqlite:///{tmp_path / 'awi.db'}"
    env = {
        **os.environ,
        "DATABASE_URL": database_url,
        "STATE_BACKEND": "memory",
        "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
    }
    command = [sys.executable, __file__, str(tmp_path), crash]
    stopped = await asyncio.to_thread(
        subprocess.run,
        [*command, "start"],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert stopped.returncode == 71, stopped.stderr
    recovered = await asyncio.to_thread(
        subprocess.run,
        [*command, "recover"],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert recovered.returncode == 0, recovered.stderr
    result = json.loads((tmp_path / "result.json").read_text())
    assert result == {
        "debits": 1,
        "spent": "3.00000000",
        "effects": 0 if crash == "before_callback" else 1,
        "replay": crash == "after_receipt",
        "needs_review": 0 if crash == "after_receipt" else 1,
    }


async def worker(directory, crash, phase):
    from datetime import timedelta
    from decimal import Decimal
    from fastapi import HTTPException
    from sqlalchemy import select
    from app.core.auth import AuthContext
    from app.core.time import utc_now
    from app.db.database import init_db, close_db, get_session_factory
    from app.db.models import WalletModel, PermitModel, LedgerEntryModel
    from app.schemas.trust import PermitCreateRequest
    from app.services.api_key_service import get_api_key_service
    from app.services.awi_http_governance import (
        begin_awi_http_governed,
        complete_awi_http_governed,
    )
    from app.services.idempotency import get_idempotency_service
    from app.services.permits import get_permit_service

    await init_db()
    state_path = directory / "identity.json"
    if phase == "start":
        wallet = "synthetic-awi-crash"
        async with get_session_factory()() as db:
            db.add(
                WalletModel(
                    wallet_id=wallet, wallet_type="agent", balance=Decimal("100")
                )
            )
            await db.commit()
        key = await get_api_key_service().create_key(wallet)
        permit = await get_permit_service().create_permit(
            PermitCreateRequest(
                issuer_wallet_id=wallet,
                subject_wallet_id=wallet,
                subject_key_id=key["key_id"],
                allowed_tools=["awi_execute"],
                scopes=["tool:awi_execute:invoke", "billing:charge"],
                max_credits=Decimal("3"),
                expires_at=utc_now() + timedelta(minutes=10),
            ),
            subject_key_id=key["key_id"],
        )
        identity = {
            "wallet": wallet,
            "key_id": key["key_id"],
            "permit_id": permit.permit_id,
        }
        state_path.write_text(json.dumps(identity))
    else:
        identity = json.loads(state_path.read_text())
    arguments = dict(
        auth=AuthContext(
            source="api_key",
            raw_key="synthetic",
            wallet_id=identity["wallet"],
            key_id=identity["key_id"],
        ),
        wallet_id=identity["wallet"],
        tool_name="awi_execute",
        endpoint="POST /v1/awi/execute",
        permit_id=identity["permit_id"],
        idempotency_key_lines=["process-crash"],
        request_payload={"action": "synthetic"},
    )
    effects = directory / "effects.txt"
    if phase == "start":
        context = await begin_awi_http_governed(**arguments)
        if crash == "before_callback":
            os._exit(71)
        context.dispatch_started = True
        with effects.open("a") as output:
            output.write("effect\n")
            output.flush()
            os.fsync(output.fileno())
        if crash == "after_callback":
            os._exit(71)

        async def interrupt_completion(**kwargs):
            os._exit(71)

        get_idempotency_service().complete = interrupt_completion
        await complete_awi_http_governed(
            context,
            request_payload=arguments["request_payload"],
            response_payload={"status": "success"},
        )
        raise AssertionError("crash did not fire")
    _, needs_review = await get_idempotency_service().reconcile_stuck_records(
        idle_seconds=0
    )
    replay = False
    try:
        context = await begin_awi_http_governed(**arguments)
        assert context.replay_response and context.replay_response["reconciled"]
        replay = True
    except HTTPException as exc:
        assert exc.status_code == 409
    async with get_session_factory()() as db:
        debits = (
            (
                await db.execute(
                    select(LedgerEntryModel).where(
                        LedgerEntryModel.wallet_id == identity["wallet"],
                        LedgerEntryModel.amount < 0,
                    )
                )
            )
            .scalars()
            .all()
        )
        permit = await db.get(PermitModel, identity["permit_id"])
    (directory / "result.json").write_text(
        json.dumps(
            {
                "debits": len(debits),
                "spent": str(permit.spent_credits),
                "effects": len(effects.read_text().splitlines())
                if effects.exists()
                else 0,
                "replay": replay,
                "needs_review": needs_review,
            }
        )
    )
    await close_db()


if __name__ == "__main__":
    asyncio.run(worker(Path(sys.argv[1]), sys.argv[2], sys.argv[3]))
