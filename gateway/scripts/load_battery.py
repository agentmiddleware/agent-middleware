#!/usr/bin/env python3
"""Load battery: concurrent governed invokes with Postgres, throughput + latency metrics.

Run:
    DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/load_test \
    python scripts/load_battery.py

Or let the script spin up its own Postgres container.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from statistics import mean
from typing import Any

# --- Bootstrap repo path ---
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("VALID_API_KEYS", "test-key")
os.environ.setdefault("TRUST_MODE_ENABLED", "false")
os.environ.setdefault("ALLOW_LEGACY_UNPERMITTED_MCP", "true")

# These must be set before importing app modules
DB_URL = os.environ.get("DATABASE_URL")
PG_CONTAINER = "agent-middleware-load-test"
_OWNED_CONTAINER_ID: str | None = None


def _ensure_postgres() -> str:
    """Start a local Postgres container if DATABASE_URL not set."""
    global DB_URL, _OWNED_CONTAINER_ID
    if DB_URL:
        return DB_URL

    # Check if container already running
    result = subprocess.run(
        ["docker", "ps", "-q", "-f", f"name={PG_CONTAINER}"],
        capture_output=True,
        text=True,
    )
    if result.stdout.strip():
        print(f"[load] Reusing existing container {PG_CONTAINER}")
    else:
        print(f"[load] Starting Postgres container {PG_CONTAINER}...")
        started = subprocess.run(
            [
                "docker",
                "run",
                "-d",
                "--rm",
                "-e",
                "POSTGRES_USER=postgres",
                "-e",
                "POSTGRES_PASSWORD=postgres",
                "-e",
                "POSTGRES_DB=load_test",
                "-p",
                "5432:5432",
                "--name",
                PG_CONTAINER,
                "postgres:16-alpine",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        _OWNED_CONTAINER_ID = started.stdout.strip()
        # Wait for Postgres to be ready
        for _ in range(30):
            check = subprocess.run(
                ["docker", "exec", _OWNED_CONTAINER_ID, "pg_isready", "-U", "postgres"],
                capture_output=True,
            )
            if check.returncode == 0:
                break
            time.sleep(0.5)
        else:
            raise RuntimeError("Postgres failed to start")
        print("[load] Postgres ready")

    DB_URL = "postgresql+asyncpg://postgres:postgres@localhost:5432/load_test"
    os.environ["DATABASE_URL"] = DB_URL
    os.environ["STATE_BACKEND"] = "postgres"
    return DB_URL


def _stop_postgres() -> None:
    """Stop the container we started."""
    global _OWNED_CONTAINER_ID
    if not _OWNED_CONTAINER_ID:
        return
    print(f"[load] Stopping container {_OWNED_CONTAINER_ID}...")
    stopped = subprocess.run(
        ["docker", "stop", "-t", "5", _OWNED_CONTAINER_ID],
        capture_output=True,
    )
    if stopped.returncode == 0:
        _OWNED_CONTAINER_ID = None


def _run_migrations() -> None:
    """Apply alembic migrations."""
    print("[load] Running alembic migrations...")
    result = subprocess.run(
        ["alembic", "upgrade", "head"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(result.stdout)
        print(result.stderr)
        raise RuntimeError("Alembic migration failed")
    print("[load] Migrations applied")


@dataclass
class LoadResult:
    concurrency: int
    total_requests: int
    success_count: int
    error_count: int
    denied_count: int
    total_credits_charged: Decimal
    latencies_ms: list[float] = field(default_factory=list)
    idempotency_violations: list[str] = field(default_factory=list)
    budget_anomalies: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return (
            self.total_requests > 0
            and self.success_count == self.total_requests
            and self.error_count == 0
            and self.denied_count == 0
            and not self.idempotency_violations
            and not self.budget_anomalies
        )

    @property
    def throughput_rps(self) -> float:
        if not self.latencies_ms:
            return 0.0
        total_seconds = sum(self.latencies_ms) / 1000
        return self.total_requests / total_seconds if total_seconds > 0 else 0.0

    @property
    def p50_ms(self) -> float:
        if not self.latencies_ms:
            return 0.0
        s = sorted(self.latencies_ms)
        idx = int(len(s) * 0.5)
        return s[idx]

    @property
    def p95_ms(self) -> float:
        if not self.latencies_ms:
            return 0.0
        s = sorted(self.latencies_ms)
        idx = int(len(s) * 0.95)
        return s[idx] if idx < len(s) else s[-1]

    @property
    def p99_ms(self) -> float:
        if not self.latencies_ms:
            return 0.0
        s = sorted(self.latencies_ms)
        idx = int(len(s) * 0.99)
        return s[idx] if idx < len(s) else s[-1]

    @property
    def mean_ms(self) -> float:
        return mean(self.latencies_ms) if self.latencies_ms else 0.0

    @property
    def min_ms(self) -> float:
        return min(self.latencies_ms) if self.latencies_ms else 0.0

    @property
    def max_ms(self) -> float:
        return max(self.latencies_ms) if self.latencies_ms else 0.0


def _fmt_latency(result: LoadResult) -> str:
    return (
        f"  min={result.min_ms:.1f}ms  p50={result.p50_ms:.1f}ms  "
        f"mean={result.mean_ms:.1f}ms  p95={result.p95_ms:.1f}ms  p99={result.p99_ms:.1f}ms  max={result.max_ms:.1f}ms"
    )


async def _run_battery(concurrency: int, total_requests: int) -> LoadResult:
    """Fire concurrent governed invokes and collect metrics."""
    import httpx
    from app.db.database import get_session_factory
    from app.db.models import PermitModel, ReceiptModel
    from app.main import app
    from app.schemas.billing import ServiceCategory
    from app.services.service_registry import get_service_registry
    from tests.test_trust_helpers import BOOTSTRAP_HEADERS, provision_agent_wallet

    tool_name = f"load-bench-{concurrency}"

    def echo(input: str = "ok") -> dict:
        return {"message": input}

    get_service_registry().register_local(
        service_id=tool_name,
        name="Load Bench Tool",
        description="Load test tool",
        category=ServiceCategory.AGENT_COMMS,
        func=echo,
        credits_per_unit=1.0,
        unit_name="call",
    )

    transport = httpx.ASGITransport(app=app)
    client = httpx.AsyncClient(transport=transport, base_url="http://test")

    try:
        # Provision wallet
        provisioned = await provision_agent_wallet(client)
        wallet_id = provisioned["agent_wallet_id"]
        key_id = provisioned["key_id"]
        agent_headers = provisioned["agent_headers"]

        # Create permit with generous budget
        permit_resp = await client.post(
            "/v1/permits",
            json={
                "issuer_wallet_id": wallet_id,
                "subject_wallet_id": wallet_id,
                "subject_key_id": key_id,
                "allowed_tools": [tool_name],
                "scopes": [f"tool:{tool_name}:invoke", "billing:charge"],
                "max_credits": total_requests * 2,
                "expires_at": (
                    datetime.now(timezone.utc) + timedelta(hours=1)
                ).isoformat(),
            },
            headers={
                **BOOTSTRAP_HEADERS,
                "Idempotency-Key": f"load-permit-{concurrency}",
            },
        )
        assert permit_resp.status_code == 201
        permit_id = permit_resp.json()["permit_id"]

        result = LoadResult(
            concurrency=concurrency,
            total_requests=total_requests,
            success_count=0,
            error_count=0,
            denied_count=0,
            total_credits_charged=Decimal("0"),
        )
        receipt_ids: list[str] = []
        idem_keys: list[str] = []
        sem = asyncio.Semaphore(concurrency)

        async def fire(i: int) -> tuple[float, dict[str, Any] | None, str]:
            async with sem:
                idem_key = f"load-{concurrency}-{i}"
                idem_keys.append(idem_key)
                start = time.perf_counter()
                resp = await client.post(
                    "/mcp/messages",
                    json={
                        "jsonrpc": "2.0",
                        "id": i,
                        "method": "tools/call",
                        "params": {
                            "name": tool_name,
                            "arguments": {"input": f"req-{i}"},
                            "mcpContext": {
                                "wallet_id": wallet_id,
                                "permit_id": permit_id,
                                "idempotency_key": idem_key,
                            },
                        },
                    },
                    headers=agent_headers,
                )
                elapsed_ms = (time.perf_counter() - start) * 1000
                body = resp.json() if resp.status_code == 200 else None
                return elapsed_ms, body, idem_key

        # Launch all requests
        tasks = [asyncio.create_task(fire(i)) for i in range(total_requests)]
        outcomes = await asyncio.gather(*tasks)

        for elapsed_ms, body, idem_key in outcomes:
            result.latencies_ms.append(elapsed_ms)
            if body is None:
                result.error_count += 1
                continue
            if "error" in body:
                result.denied_count += 1
                receipt = body.get("error", {}).get("data", {}).get("receipt")
                if receipt:
                    receipt_ids.append(receipt["receipt_id"])
                    result.total_credits_charged += Decimal(
                        str(receipt.get("credits_charged", 0))
                    )
            else:
                result.success_count += 1
                receipt = body["result"]["receipt"]
                receipt_ids.append(receipt["receipt_id"])
                result.total_credits_charged += Decimal(str(receipt["credits_charged"]))

        # Post-hoc checks
        factory = get_session_factory()
        async with factory() as session:
            permit_model = await session.get(PermitModel, permit_id)
            if permit_model:
                expected_spent = Decimal(str(result.success_count))
                if permit_model.spent_credits != expected_spent:
                    result.budget_anomalies.append(
                        f"Permit spent_credits mismatch: expected {expected_spent}, got {permit_model.spent_credits}"
                    )

            # Check for duplicate receipts (idempotency violation)
            receipt_outcomes = Counter()
            for rid in receipt_ids:
                rec = await session.get(ReceiptModel, rid)
                if rec:
                    receipt_outcomes[rid] += 1

            # Verify all receipts are unique
            seen: set[str] = set()
            for rid in receipt_ids:
                if rid in seen:
                    result.idempotency_violations.append(f"Duplicate receipt_id: {rid}")
                seen.add(rid)

        return result

    finally:
        await client.aclose()
        get_service_registry().unregister_local(tool_name)


async def main() -> int:
    print("=" * 60)
    print("Agent Middleware API — Load Battery")
    print("=" * 60)

    _ensure_postgres()
    _run_migrations()

    # Import app modules only after env is configured
    from app.db.database import init_db, close_db

    await init_db()

    results: list[LoadResult] = []

    try:
        for concurrency in (10, 50, 100):
            total = concurrency * 5  # 5 batches per concurrency level
            print(f"\n[load] Running {total} requests at concurrency={concurrency}...")
            result = await _run_battery(concurrency, total)
            results.append(result)
            print(
                f"  success={result.success_count}  denied={result.denied_count}  errors={result.error_count}"
            )
            print(f"  throughput={result.throughput_rps:.1f} req/s")
            print(_fmt_latency(result))
            if result.idempotency_violations:
                print(
                    f"  ⚠️ idempotency violations: {len(result.idempotency_violations)}"
                )
                for v in result.idempotency_violations:
                    print(f"    - {v}")
            if result.budget_anomalies:
                print(f"  ⚠️ budget anomalies: {len(result.budget_anomalies)}")
                for a in result.budget_anomalies:
                    print(f"    - {a}")
    finally:
        await close_db()

    # Generate report
    report_path = REPO_ROOT / "reports" / "load_battery_report.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)

    report = f"""# Load Battery Report

Generated: {datetime.now(timezone.utc).isoformat()}
Database: PostgreSQL

## Results

| Concurrency | Requests | Success | Denied | Errors | Throughput (req/s) | p50 (ms) | p95 (ms) | p99 (ms) | Mean (ms) | Status |
|-------------|----------|---------|--------|--------|-------------------|----------|----------|----------|-----------|--------|
"""
    for r in results:
        report += (
            f"| {r.concurrency} | {r.total_requests} | {r.success_count} | "
            f"{r.denied_count} | {r.error_count} | {r.throughput_rps:.1f} | "
            f"{r.p50_ms:.1f} | {r.p95_ms:.1f} | {r.p99_ms:.1f} | {r.mean_ms:.1f} | "
            f"{'PASS' if r.passed else 'FAIL'} |\n"
        )

    report += "\n## Anomalies\n\n"
    for r in results:
        report += f"### Concurrency {r.concurrency}\n\n"
        if r.idempotency_violations:
            report += "**Idempotency violations:**\n"
            for v in r.idempotency_violations:
                report += f"- {v}\n"
        else:
            report += "- No idempotency violations detected.\n"
        if r.budget_anomalies:
            report += "\n**Budget anomalies:**\n"
            for a in r.budget_anomalies:
                report += f"- {a}\n"
        else:
            report += "- No budget anomalies detected.\n"
        report += "\n"

    report_path.write_text(report)
    print(f"\n[load] Report written to {report_path}")

    # Also print summary
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    for r in results:
        status = "✅ PASS" if r.passed else "⚠️ FAIL"
        print(f"  {r.concurrency:3d} concurrent: {status}")

    return 0 if all(r.passed for r in results) else 1


if __name__ == "__main__":
    try:
        exit_code = asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[load] Interrupted")
        exit_code = 130
    finally:
        _stop_postgres()
    sys.exit(exit_code)
