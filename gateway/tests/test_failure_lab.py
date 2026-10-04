"""The failure lab is a published claim about the baseline as much as the gateway.

CI runs it so the numbers the report prints cannot drift from the code: the
existing integration pays twice, a correctly used native idempotency key pays
once with no gateway involved, the gateway pays once at either fault hop, an
ambiguous dispatch is receipted and never repeated, and an agent restart with a
new key defeats every configuration. The saved artifacts must exist and carry
no credential material.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[1]
LATENCY_SAMPLES = 2

ADMIN_KEY = "failure-lab-admin-key"
UPSTREAM_BEARER = "failure-lab-upstream-bearer"
SIGNING_SEED = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8="


@pytest.fixture(scope="module")
def lab_run(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    output_dir = tmp_path_factory.mktemp("failure-lab")
    result = subprocess.run(
        [
            sys.executable,
            "scripts/failure_lab.py",
            "--json",
            "--output-dir",
            str(output_dir),
            "--latency-samples",
            str(LATENCY_SAMPLES),
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, result.stderr[-4000:]
    return json.loads(result.stdout)


def _scenario(summary: dict[str, Any], name: str) -> dict[str, Any]:
    for item in summary["scenarios"]:
        if item["scenario"] == name:
            return item
    raise AssertionError(f"scenario {name!r} missing from the run")


def _events(summary: dict[str, Any]) -> list[dict[str, Any]]:
    path = Path(summary["artifacts"]["events"])
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_run_is_green_and_records_its_configuration(lab_run: dict[str, Any]) -> None:
    assert lab_run["ok"] is True
    assert lab_run["schema_version"] == 1
    assert lab_run["fault"]["name"] == "downstream action completed; response lost"
    assert lab_run["software"]["agent_native_middleware"]
    assert lab_run["software"]["mcp"]
    assert lab_run["gateway_configuration"]["public_tool_id"] == "vendor.payout.send"
    assert lab_run["gateway_configuration"]["trust_mode_enabled"] is True
    assert all(item["ok"] for item in lab_run["scenarios"])


def test_existing_integration_pays_twice(lab_run: dict[str, Any]) -> None:
    """The problem the boundary exists for has to stay real in the lab."""
    item = _scenario(lab_run, "existing.lost_response")
    assert [attempt["kind"] for attempt in item["attempts"]] == [
        "response_lost",
        "success",
    ]
    assert item["downstream_effects"] == 2
    assert item["duplicate_effects"] == 1
    assert lab_run["verdicts"]["existing_integration_duplicates_the_action"] is True


def test_native_baseline_already_handles_the_fault(lab_run: dict[str, Any]) -> None:
    """The lab must not win against a correct integration, and must say so."""
    item = _scenario(lab_run, "native.lost_response")
    assert [attempt["kind"] for attempt in item["attempts"]] == [
        "response_lost",
        "success",
    ]
    assert item["attempts"][1]["replayed_by_downstream"] is True
    assert item["downstream_effects"] == 1
    assert item["unresolved_outcomes"] == 0
    assert lab_run["verdicts"]["native_baseline_already_handles_this_fault"] is True
    assert lab_run["verdicts"]["gateway_reduces_effects_vs_native_baseline"] is False
    assert lab_run["comparison"]["downstream_effects"]["native"] == 1
    assert lab_run["comparison"]["downstream_effects"]["gateway_agent_hop"] == 1

    report = Path(lab_run["artifacts"]["report"]).read_text()
    assert "already prevents the duplicate for this fault" in report
    assert "Remaining limitations:" in report


@pytest.mark.parametrize(
    "scenario",
    ["gateway.lost_response.agent_hop", "gateway_native.lost_response.agent_hop"],
)
def test_gateway_pays_once_when_the_agent_loses_the_response(
    lab_run: dict[str, Any], scenario: str
) -> None:
    item = _scenario(lab_run, scenario)
    assert [attempt["kind"] for attempt in item["attempts"]] == [
        "response_lost",
        "success",
    ]
    assert item["downstream_effects"] == 1
    assert item["gateway_dispatches"] == 1
    assert item["debits"] == 1
    assert item["refunds"] == 0
    assert len(item["receipt_ids"]) == 1
    assert item["receipts_verified_offline"] is True

    # The retry handed back exactly what the lost response carried: the
    # harness saw that response go missing and recorded it.
    lost = [
        event["drop"]
        for event in _events(lab_run)
        if event["kind"] == "response_lost" and event["scenario"] == scenario
    ]
    assert len(lost) == 1
    assert lost[0]["hop"] == "agent->gateway"
    assert lost[0]["lost_receipt_id"] == item["attempts"][1]["receipt_id"]
    assert lost[0]["lost_confirmation"] == item["attempts"][1]["confirmation"]


@pytest.mark.parametrize(
    "scenario",
    [
        "gateway.lost_response.downstream_hop",
        "gateway_native.lost_response.downstream_hop",
    ],
)
def test_gateway_preserves_ambiguity_and_never_redispatches(
    lab_run: dict[str, Any], scenario: str
) -> None:
    item = _scenario(lab_run, scenario)
    kinds = [attempt["kind"] for attempt in item["attempts"]]
    assert kinds == ["delivery_uncertain", "delivery_uncertain"]
    assert item["attempts"][0]["receipt_outcome"] == "delivery_uncertain"
    assert item["attempts"][0]["receipt_id"] == item["attempts"][1]["receipt_id"]
    assert item["gateway_dispatches"] == 1
    assert item["downstream_effects"] == 1
    assert item["debits"] == 1
    assert item["refunds"] == 0
    assert item["unresolved_outcomes"] == 1
    assert item["receipts_verified_offline"] is True
    assert lab_run["verdicts"]["gateway_never_redispatched_after_ambiguity"] is True
    assert lab_run["verdicts"]["gateway_retained_the_charge_after_ambiguity"] is True


def test_forwarded_key_reaches_the_downstream(lab_run: dict[str, Any]) -> None:
    """The gateway forwards the caller's key in MCP request metadata."""
    item = _scenario(lab_run, "gateway_native.lost_response.downstream_hop")
    key = item["attempts"][0]["idempotency_key"]
    effects = [
        event
        for event in _events(lab_run)
        if event["kind"] == "downstream_effect"
        and event["scenario"] == item["scenario"]
    ]
    assert len(effects) == 1
    assert effects[0]["idempotency_key"] == key
    assert effects[0]["forwarded_meta"]["io.agentmiddleware/idempotency_key"] == key
    assert (
        item["downstream_stored_response"]["confirmation"] == effects[0]["confirmation"]
    )


def test_agent_restart_with_a_new_key_defeats_every_configuration(
    lab_run: dict[str, Any],
) -> None:
    native = _scenario(lab_run, "native.agent_restart_new_key")
    gateway = _scenario(lab_run, "gateway.agent_restart_new_key")
    assert native["downstream_effects"] == 2
    assert gateway["downstream_effects"] == 2
    assert gateway["gateway_dispatches"] == 2
    assert gateway["debits"] == 2
    assert len(gateway["receipt_ids"]) == 2
    assert (
        gateway["attempts"][0]["idempotency_key"]
        != gateway["attempts"][1]["idempotency_key"]
    )
    assert (
        lab_run["verdicts"]["agent_restart_with_a_new_key_defeats_every_configuration"]
        is True
    )


def test_changed_payload_under_a_spent_key_is_refused_everywhere(
    lab_run: dict[str, Any],
) -> None:
    native = _scenario(lab_run, "native.key_conflict")
    gateway = _scenario(lab_run, "gateway.key_conflict")
    assert native["attempts"][1]["kind"] == "downstream_idempotency_conflict"
    assert native["downstream_effects"] == 1
    assert gateway["attempts"][1]["kind"] == "idempotency_key_reused"
    assert gateway["downstream_effects"] == 1
    assert gateway["gateway_dispatches"] == 1
    assert gateway["debits"] == 1


def test_controls_succeed_so_the_boundary_is_not_merely_blocking(
    lab_run: dict[str, Any],
) -> None:
    for name in ("existing.control", "native.control", "gateway.control"):
        item = _scenario(lab_run, name)
        assert item["attempts"][0]["kind"] == "success"
        assert item["downstream_effects"] == 1
    gateway = _scenario(lab_run, "gateway.control")
    assert gateway["attempts"][0]["receipt_outcome"] == "success"
    assert gateway["receipts_verified_offline"] is True
    latency = lab_run["latency"]
    assert latency["samples"] == LATENCY_SAMPLES
    assert len(latency["direct_ms"]) == LATENCY_SAMPLES
    assert len(latency["gateway_ms"]) == LATENCY_SAMPLES
    assert latency["added_by_gateway_ms"] is not None


def test_artifacts_are_saved_and_carry_no_credentials(lab_run: dict[str, Any]) -> None:
    artifacts = lab_run["artifacts"]
    run_dir = Path(artifacts["run_dir"])
    for name in ("report", "summary", "events", "effects", "config", "gateway_db"):
        assert Path(artifacts[name]).exists(), name
        assert Path(artifacts[name]).parent == run_dir

    # One line per executed tool body, counted by the rail rather than the
    # gateway: every scenario's effects plus the fault-free latency samples.
    effect_lines = [
        line for line in Path(artifacts["effects"]).read_text().splitlines() if line
    ]
    expected = sum(item["downstream_effects"] for item in lab_run["scenarios"])
    expected += 2 * LATENCY_SAMPLES
    assert len(effect_lines) == expected

    faults = [item for item in lab_run["scenarios"] if item["fault"] != "none"]
    lost = [event for event in _events(lab_run) if event["kind"] == "response_lost"]
    assert len(lost) == len(faults)

    config = json.loads(Path(artifacts["config"]).read_text())
    assert config["software"] == lab_run["software"]
    assert [row["scenario"] for row in config["scenario_sequence"]] == [
        item["scenario"] for item in lab_run["scenarios"]
    ]

    for name in ("report", "summary", "events", "effects", "config"):
        text = Path(artifacts[name]).read_text()
        assert "b2a_" not in text, f"{name} leaks a wallet-scoped API key"
        assert ADMIN_KEY not in text, f"{name} leaks the admin key"
        assert UPSTREAM_BEARER not in text, f"{name} leaks the upstream bearer"
        assert SIGNING_SEED not in text, f"{name} leaks the signing seed"


def test_reusing_a_run_directory_is_refused(tmp_path: Path) -> None:
    """Two runs in one directory would leave the evidence self-contradicting.

    `events.jsonl` and `effects.jsonl` are append-only while the reports are
    overwritten, so a repeated `--run-id` would otherwise exit zero with an
    effects log holding twice what the report claims.
    """
    output_dir = tmp_path / "runs"
    run_dir = output_dir / "pinned"
    run_dir.mkdir(parents=True)
    (run_dir / "effects.jsonl").write_text('{"seq": 1}\n')

    result = subprocess.run(
        [
            sys.executable,
            "scripts/failure_lab.py",
            "--json",
            "--output-dir",
            str(output_dir),
            "--run-id",
            "pinned",
            "--latency-samples",
            "0",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode != 0
    assert "already holds a run" in result.stderr
    assert "--run-id" in result.stderr
    # The refusal happened before anything ran: the seeded file is untouched
    # and no report was written beside it.
    assert (run_dir / "effects.jsonl").read_text() == '{"seq": 1}\n'
    assert not (run_dir / "report.json").exists()
