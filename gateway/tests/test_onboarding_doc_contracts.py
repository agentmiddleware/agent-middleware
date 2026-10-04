"""Keep executable onboarding recipes aligned with local API contracts."""

from __future__ import annotations

import ast
import inspect
import json
from pathlib import Path
import re
import shutil
import subprocess


ROOT = Path(__file__).resolve().parents[1]


def read(name):
    return (ROOT / "docs" / name).read_text()


def test_golden_path_dogfood_arguments_bind_without_invoking_tool():
    doc = read("golden-path.md")
    replacement = re.search(r'with `(\{"text": "hello"\})`', doc)
    assert replacement, "The stock dogfood substitution must include its arguments"
    tree = ast.parse((ROOT / "app/services/dogfood_tool.py").read_text())
    function = next(
        n
        for n in tree.body
        if isinstance(n, ast.FunctionDef) and n.name == "_write_note"
    )
    function.body = [ast.Pass()]
    module = ast.Module(body=[function], type_ignores=[])
    ast.fix_missing_locations(module)
    scope = {}
    exec(compile(module, "dogfood-signature", "exec"), scope)
    inspect.signature(scope["_write_note"]).bind(**json.loads(replacement[1]))
    assert "both the first invoke and its replay" in doc


def test_partner_install_has_verification_extra_for_source_and_wheel():
    doc = read("partner-first-tool-runbook.md")
    assert 'pip install "../sdk/python[verify]"' in doc
    assert 'pip install "./b2a_sdk-<version>-py3-none-any.whl[verify]"' in doc


def test_partner_verification_uses_post_body_and_auth():
    doc = read("partner-first-tool-runbook.md")
    step = doc.split("5. Show ledger debit", 1)[1].split("6. Replay", 1)[0]
    assert "POST /v1/receipts/verify" in step
    assert "X-API-Key: $AGENT_API_KEY" in step
    assert "receipt_id" in step and "$RECEIPT_ID" in step
    assert "GET /v1/receipts/verify" not in step


def test_review_claim_names_local_receipt_exception():
    row = next(
        line
        for line in read("security-review-kit.md").splitlines()
        if line.startswith("| 3 |")
    )
    assert "local" in row and "manual review" in row and "without a receipt" in row


def test_ci_recipe_exports_one_provisioned_identity(tmp_path):
    doc = read("constant-test-loop.md")
    recipe = doc.split("# Provision an agent key once", 1)[1].split(
        "# Or use a restrictive", 1
    )[0]
    recipe = "# Provision an agent key once" + recipe
    fake_python = tmp_path / "python"
    fake_python.write_text(
        "#!/bin/sh\n"
        'count=0; [ ! -f "$COUNT_FILE" ] || count=$(cat "$COUNT_FILE")\n'
        'count=$((count + 1)); printf "%s" "$count" > "$COUNT_FILE"\n'
        'printf \'{"api_key":"synthetic-%s","wallet_id":"wallet-%s","key_id":"key-%s"}\\n\' "$count" "$count" "$count"\n'
    )
    fake_python.chmod(0o755)
    jq = shutil.which("jq")
    assert jq, "jq is needed to verify this documented recipe"
    env = {
        "PATH": f"{tmp_path}:{Path(jq).parent}:/usr/bin:/bin",
        "COUNT_FILE": str(tmp_path / "count"),
    }
    result = subprocess.run(
        [
            "/bin/sh",
            "-c",
            recipe
            + '\nprintf "%s\\n" "$CI_SMOKE_AGENT_KEY" "$CI_SMOKE_WALLET_ID" "$CI_SMOKE_KEY_ID"',
        ],
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    assert result.stdout.splitlines() == ["synthetic-1", "wallet-1", "key-1"]
    assert (tmp_path / "count").read_text() == "1"


def test_fast_tier_quick_command_targets_suite():
    doc = read("failure-lab-suite.md").split("## 8. Running it yourself", 1)[1]
    target = next(line.split()[1] for line in doc.splitlines() if "# fast tier" in line)
    makefile = (ROOT / "Makefile").read_text()
    command = makefile.split(target + ":", 1)[1].split("\n\n", 1)[0]
    assert "--tier fast" in command
