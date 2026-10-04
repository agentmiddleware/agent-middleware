"""Regression tests for fail-closed invariant-attack verdict semantics."""

from __future__ import annotations

import ast
import importlib
import io
import json
import sys
import threading
from collections import Counter
from pathlib import Path

import pytest


ATTACK_DIR = Path(__file__).resolve().parents[1] / "scripts" / "invariant_attacks"
sys.path.insert(0, str(ATTACK_DIR))

from attacklib import verdict_exit_code  # noqa: E402

import attack4_forgery as attack4  # noqa: E402
import attack5_crash_sqlite as attack5  # noqa: E402
import attack6_key_misuse as attack6  # noqa: E402
import attack_combined as combined  # noqa: E402
import redact_evidence as redactor  # noqa: E402


def _nodes_without_nested_scopes(node: ast.AST) -> list[ast.AST]:
    nodes = [node]
    if isinstance(
        node,
        (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef),
    ):
        return nodes
    for child in ast.iter_child_nodes(node):
        nodes.extend(_nodes_without_nested_scopes(child))
    return nodes


def _is_namespaced_call(node: ast.AST | None, namespace: str, function: str) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == namespace
        and node.func.attr == function
    )


def _printed_evidence(statement: ast.stmt) -> ast.AST | None:
    if not (
        isinstance(statement, ast.Expr)
        and isinstance(statement.value, ast.Call)
        and isinstance(statement.value.func, ast.Name)
        and statement.value.func.id == "print"
        and len(statement.value.args) == 1
        and not statement.value.keywords
    ):
        return None
    serialized = statement.value.args[0]
    if not (_is_namespaced_call(serialized, "json", "dumps") and serialized.args):
        return None
    return serialized.args[0]


def _written_evidence(statement: ast.stmt) -> ast.AST | None:
    if not isinstance(statement, ast.With):
        return None

    matches: list[ast.AST] = []
    for item in statement.items:
        opened = item.context_expr
        if not (
            isinstance(opened, ast.Call)
            and isinstance(opened.func, ast.Name)
            and opened.func.id == "open"
            and opened.args
            and isinstance(item.optional_vars, ast.Name)
        ):
            continue
        mode = (
            opened.args[1]
            if len(opened.args) > 1
            else next(
                (keyword.value for keyword in opened.keywords if keyword.arg == "mode"),
                None,
            )
        )
        if not (isinstance(mode, ast.Constant) and mode.value == "w"):
            continue

        for body_statement in statement.body:
            if not (
                isinstance(body_statement, ast.Expr)
                and _is_namespaced_call(body_statement.value, "json", "dump")
            ):
                continue
            dumped = body_statement.value
            if not (
                len(dumped.args) >= 2
                and isinstance(dumped.args[1], ast.Name)
                and dumped.args[1].id == item.optional_vars.id
            ):
                continue
            matches.append(dumped.args[0])

    return matches[0] if len(matches) == 1 else None


def _is_main_guard(test: ast.AST) -> bool:
    if not (
        isinstance(test, ast.Compare)
        and len(test.ops) == 1
        and isinstance(test.ops[0], ast.Eq)
        and len(test.comparators) == 1
    ):
        return False
    operands = (test.left, test.comparators[0])
    return (
        isinstance(operands[0], ast.Name)
        and operands[0].id == "__name__"
        and isinstance(operands[1], ast.Constant)
        and operands[1].value == "__main__"
    ) or (
        isinstance(operands[1], ast.Name)
        and operands[1].id == "__name__"
        and isinstance(operands[0], ast.Constant)
        and operands[0].value == "__main__"
    )


@pytest.mark.parametrize(
    ("verdict", "expected"),
    [
        ("HELD", 0),
        ("BROKE", 1),
        ("PARTIAL", 1),
        ("UNKNOWN", 1),
        ("held", 1),
        ("", 1),
        (None, 1),
    ],
)
def test_verdict_exit_code_fails_closed(verdict: str | None, expected: int) -> None:
    assert verdict_exit_code(verdict) == expected  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "script_name",
    [
        "attack1_double_charge.py",
        "attack2_budget.py",
        "attack3_scope.py",
        "attack4_forgery.py",
        "attack2_budget_postgres.py",
        "attack2_mechanism_sqlite.py",
    ],
)
def test_attack_main_returns_shared_verdict_exit_code(script_name: str) -> None:
    tree = ast.parse((ATTACK_DIR / script_name).read_text(encoding="utf-8"))
    main_candidates = [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "main"
    ]
    assert len(main_candidates) == 1
    main = main_candidates[0]
    assert isinstance(main, ast.FunctionDef)

    final_statement = main.body[-1]
    assert isinstance(final_statement, ast.Return)
    assert ast.dump(final_statement.value) == ast.dump(
        ast.Call(
            func=ast.Attribute(
                value=ast.Name(id="A", ctx=ast.Load()),
                attr="verdict_exit_code",
                ctx=ast.Load(),
            ),
            args=[ast.Name(id="verdict", ctx=ast.Load())],
            keywords=[],
        )
    )

    main_scope = [
        node
        for statement in main.body
        for node in _nodes_without_nested_scopes(statement)
    ]
    assert [node for node in main_scope if isinstance(node, ast.Return)] == [
        final_statement
    ]
    assert not any(isinstance(node, (ast.Yield, ast.YieldFrom)) for node in main_scope)
    assert not any(_is_namespaced_call(node, "sys", "exit") for node in main_scope)

    printed = [
        (index, payload)
        for index, statement in enumerate(main.body[:-1])
        if (payload := _printed_evidence(statement)) is not None
    ]
    written = [
        (index, payload)
        for index, statement in enumerate(main.body[:-1])
        if (payload := _written_evidence(statement)) is not None
    ]
    assert len(printed) == 1
    print_index, printed_payload = printed[0]
    matching_writes = [
        (index, payload)
        for index, payload in written
        if ast.dump(payload) == ast.dump(printed_payload)
    ]
    assert len(matching_writes) == 1
    write_index, _written_payload = matching_writes[0]
    assert print_index < write_index < len(main.body) - 1

    guards = [
        (index, node)
        for index, node in enumerate(tree.body)
        if isinstance(node, ast.If) and _is_main_guard(node.test)
    ]
    assert len(guards) == 1
    guard_index, guard = guards[0]
    assert guard_index == len(tree.body) - 1
    assert guard_index > tree.body.index(main)
    assert guard.orelse == []
    assert len(guard.body) == 1
    assert ast.dump(guard.body[0]) == ast.dump(
        ast.Expr(
            value=ast.Call(
                func=ast.Attribute(
                    value=ast.Name(id="sys", ctx=ast.Load()),
                    attr="exit",
                    ctx=ast.Load(),
                ),
                args=[
                    ast.Call(
                        func=ast.Name(id="main", ctx=ast.Load()),
                        args=[],
                        keywords=[],
                    )
                ],
                keywords=[],
            )
        )
    )


def _import_fresh(module_name: str):
    """Import an attack script anew so module-level code runs under stubs."""
    sys.modules.pop(module_name, None)
    return importlib.import_module(module_name)


def _stub_budget_race(monkeypatch, *, successes_for, spent_for=None) -> None:
    """Stub the attacklib HTTP/DB primitives the attack 2 race scripts use.

    ``successes_for(cap)`` decides how many parallel calls succeed (the rest
    are budget-denied); each success debits 2 credits on the wallet ledger.
    ``spent_for(successes)`` overrides what the permit row's spent_credits
    says, which otherwise agrees with the ledger.
    """
    import attacklib

    state: dict = {}

    def provision(label):
        return {"api_key": f"b2a_{label}", "wallet_id": f"agt-{label}"}

    def issue_permit(cred, *, max_credits, **_kwargs):
        state["cap"] = max_credits
        return {"json": {"permit_id": f"permit-{cred['wallet_id']}"}}

    def fire_parallel(n, _fn):
        won = successes_for(state["cap"])
        state["successes"] = won
        return [{"outcome": "success"}] * won + [
            {"reason": "permit_budget_exceeded"}
        ] * (n - won)

    def charged(successes):
        return successes * 2.0

    def ledger(_cred):
        return {"json": {"period_debits_exact": str(charged(state["successes"]))}}

    def db_rows(query, _params=()):
        if "FROM permits" in query:
            spent = (spent_for or charged)(state["successes"])
            return [
                {
                    "permit_id": "permit-x",
                    "max_credits": state["cap"],
                    "spent_credits": spent,
                    "status": "active",
                }
            ]
        return [{"amount": -2}] * state["successes"]

    monkeypatch.setattr(attacklib, "API", attacklib.API)
    monkeypatch.setattr(attacklib, "provision", provision)
    monkeypatch.setattr(attacklib, "issue_permit", issue_permit)
    monkeypatch.setattr(attacklib, "fire_parallel", fire_parallel)
    monkeypatch.setattr(attacklib, "ledger", ledger)
    monkeypatch.setattr(attacklib, "db_rows", db_rows)
    monkeypatch.setattr(attacklib, "outcome_of", lambda r: r.get("outcome"))
    monkeypatch.setattr(attacklib, "reason_of", lambda r: r.get("reason"))
    monkeypatch.setattr(attacklib, "invoke", lambda *a, **k: {})


def _held_race(cap):
    return cap // 2


def _overspent_race(cap):
    return cap // 2 + 1


def _vacuous_race(_cap):
    return 0


@pytest.mark.parametrize(
    "script_name", ["attack2_budget_postgres", "attack2_mechanism_sqlite"]
)
@pytest.mark.parametrize(
    ("successes_for", "expected_verdict", "expected_exit"),
    [
        pytest.param(_held_race, "HELD", 0, id="cap-held"),
        pytest.param(_overspent_race, "BROKE", 1, id="overspent"),
        # Every call failing for some other reason debits nothing, so nothing
        # is overspent -- but the race never reached the cap, which proves
        # nothing and must not pass.
        pytest.param(_vacuous_race, "BROKE", 1, id="vacuous-race"),
    ],
)
def test_attack2_race_scripts_fail_closed(
    monkeypatch,
    tmp_path,
    capsys,
    script_name: str,
    successes_for,
    expected_verdict: str,
    expected_exit: int,
) -> None:
    _stub_budget_race(monkeypatch, successes_for=successes_for)
    monkeypatch.chdir(tmp_path)

    module = _import_fresh(script_name)
    assert module.main() == expected_exit

    printed = json.loads(capsys.readouterr().out)
    assert printed["verdict"] == expected_verdict
    (evidence_file,) = tmp_path.glob("evidence_attack2_*.json")
    assert json.loads(evidence_file.read_text()) == printed


def test_attack2_mechanism_flags_lost_update_without_overspend(
    monkeypatch, tmp_path, capsys
) -> None:
    # The permit row under-records spend (a lost update) even though the cap
    # held on the ledger: the mechanism check must still fail.
    _stub_budget_race(
        monkeypatch, successes_for=_held_race, spent_for=lambda _successes: 2.0
    )
    monkeypatch.chdir(tmp_path)

    module = _import_fresh("attack2_mechanism_sqlite")
    assert module.main() == 1
    printed = json.loads(capsys.readouterr().out)
    assert printed["lost_update"] is True
    assert printed["overspent_vs_cap"] is False
    assert printed["verdict"] == "BROKE"


def test_combined_requires_every_storm_variant_to_start() -> None:
    started = Counter({tag: 1 for tag in combined.REQUIRED_STORM_VECTOR_TAGS})

    assert combined.all_required_storm_vectors_started(started)


@pytest.mark.parametrize("missing", combined.REQUIRED_STORM_VECTOR_TAGS)
def test_combined_fails_closed_when_storm_variant_did_not_start(missing: str) -> None:
    started = Counter({tag: 1 for tag in combined.REQUIRED_STORM_VECTOR_TAGS})
    del started[missing]

    assert not combined.all_required_storm_vectors_started(started)


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        (
            {
                "crash_happened": True,
                "no_double_by_total": True,
                "no_double_by_key": True,
                "no_receipt_without_charge": True,
                "silent_orphan_count": 0,
                "proof_pending_count": 0,
                "worker_error_count": 0,
            },
            "HELD",
        ),
        (
            {
                "crash_happened": True,
                "no_double_by_total": True,
                "no_double_by_key": True,
                "no_receipt_without_charge": True,
                "silent_orphan_count": 0,
                "proof_pending_count": 1,
                "worker_error_count": 0,
            },
            "PARTIAL",
        ),
        (
            {
                "crash_happened": True,
                "no_double_by_total": True,
                "no_double_by_key": True,
                "no_receipt_without_charge": True,
                "silent_orphan_count": 1,
                "proof_pending_count": 0,
                "worker_error_count": 0,
            },
            "BROKE",
        ),
        (
            {
                "crash_happened": False,
                "no_double_by_total": True,
                "no_double_by_key": True,
                "no_receipt_without_charge": True,
                "silent_orphan_count": 0,
                "proof_pending_count": 0,
                "worker_error_count": 0,
            },
            "BROKE",
        ),
    ],
)
def test_attack5_verdict_distinguishes_pending_from_corruption(
    kwargs: dict, expected: str
) -> None:
    assert attack5.classify_verdict(**kwargs) == expected


@pytest.mark.parametrize(
    ("refund", "expected_correlated", "expected_orphan"),
    [
        (
            {
                "entry_id": "refund-ledger-1",
                "amount": 2,
                "correlation_id": "ledger-1",
            },
            1,
            0,
        ),
        (None, 0, 1),
        (
            {
                "entry_id": "refund-wrong-ledger",
                "amount": 2,
                "correlation_id": "ledger-1",
            },
            0,
            1,
        ),
        (
            {
                "entry_id": "refund-ledger-1",
                "amount": 3,
                "correlation_id": "ledger-1",
            },
            0,
            1,
        ),
    ],
)
def test_attack5_refunds_must_correlate_to_the_exact_debit(
    monkeypatch, refund, expected_correlated: int, expected_orphan: int
) -> None:
    def fake_db_rows(query: str, params=()):
        if "action='debit'" in query:
            return [{"entry_id": "ledger-1", "amount": -2, "operation_key": "idem-1"}]
        if "action='refund'" in query:
            return [refund] if refund else []
        return []

    monkeypatch.setattr(attack5.A, "db_rows", fake_db_rows)

    analysis = attack5.ledger_analysis("agt-test", {"idem-1"})

    assert analysis["correlated_refunded_debits"] == expected_correlated
    assert analysis["silent_orphan_debits"] == expected_orphan


def test_attack5_worker_failure_prevents_held_verdict() -> None:
    launch_hist = Counter()
    worker_errors = Counter()

    def fail_request():
        raise RuntimeError("injected worker failure")

    attack5.run_worker_request(
        "idem-failed",
        fail_request,
        launch_hist,
        worker_errors,
        threading.Lock(),
    )

    assert worker_errors == {"idem-failed": 1}
    assert (
        attack5.classify_verdict(
            crash_happened=True,
            no_double_by_total=True,
            no_double_by_key=True,
            no_receipt_without_charge=True,
            silent_orphan_count=0,
            proof_pending_count=0,
            worker_error_count=sum(worker_errors.values()),
        )
        == "BROKE"
    )


def test_combined_treats_linux_zombie_as_stopped(monkeypatch) -> None:
    monkeypatch.setattr(
        combined,
        "open",
        lambda *_args, **_kwargs: io.StringIO("4321 (python worker) Z 1 2 3"),
        raising=False,
    )

    def unexpected_signal_probe(_pid, _signal):
        raise AssertionError("zombie must be classified before the signal probe")

    monkeypatch.setattr(combined.os, "kill", unexpected_signal_probe)

    assert not combined.alive(4321)


def test_combined_proc_stat_parser_handles_comm_parenthesis_and_malformed_input() -> (
    None
):
    assert combined.proc_stat_state("4321 (python) worker) Z 1 2 3") == "Z"
    assert combined.proc_stat_state("malformed proc stat without delimiter") is None


def test_attack6_requires_exact_status_and_reason() -> None:
    expected = {"status": 403, "json": {"detail": {"error": "invalid_api_key"}}}

    assert attack6.matches_response(expected, status=403, reason="invalid_api_key")
    assert not attack6.matches_response(
        {**expected, "status": None}, status=403, reason="invalid_api_key"
    )
    assert not attack6.matches_response(
        {**expected, "status": 500}, status=403, reason="invalid_api_key"
    )
    assert not attack6.matches_response(
        expected, status=403, reason="missing_credentials"
    )
    assert attack6.matches_response({"status": 204}, status=204)


def test_forgery_cases_include_ledger_binding_and_fail_closed_if_missing() -> None:
    bundle = {
        "receipt": {
            "wallet_id": "agt-victim",
        },
        "signing_input": '{"ledger_entry_id":"ledger-original"}',
    }

    tampers = attack4.A.signed_receipt_tamper_cases(bundle)

    assert tampers["ledger_entry_id"] == (
        "ledger-original",
        "ledger-forged-entry",
    )
    missing = attack4.A.signed_receipt_tamper_cases({"receipt": {}})
    assert missing["ledger_entry_id"][0] == "<missing-ledger-entry-id>"


def test_evidence_redaction_removes_credentials_and_wallets() -> None:
    credential = "b2a_" + "A" * 32
    evidence = {
        "victim_wallet": "agt-secret-tenant",
        "nested": {
            "api_key": credential,
            "message": f"leaked {credential} for agt-deadbeef1234",
        },
        "receipt_id": "rcpt-public-proof-reference",
    }

    redacted = redactor.redact_evidence(evidence)

    assert redacted["victim_wallet"] == "<redacted>"
    assert redacted["nested"]["api_key"] == "<redacted>"
    assert redacted["nested"]["message"] == (
        "leaked <redacted-credential> for <redacted-wallet>"
    )
    assert redacted["receipt_id"] == "rcpt-public-proof-reference"
    assert not redactor.contains_full_credential(redacted)
    assert not redactor.contains_wallet_identifier(redacted)
