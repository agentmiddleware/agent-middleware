"""ATTACK 4 — Forged / tampered receipts.

Invariant under test: a receipt's signed facts cannot be altered without the
independent offline verifier catching it, and "forged" is reported distinctly
from "cannot verify" (unknown key). Uses the shipped b2a_sdk verifier, which
imports no server code.

Sub-tests:
  4a. Genuine SUCCESS receipt bundle verifies (exit 0 VERIFIED).
  4b. Genuine DENIAL receipt bundle verifies (denials are real signed evidence).
  4c. Tamper representative high-value signed facts (credits_charged, outcome,
      tool, wallet_id, ledger_entry_id) -> INVALID (exit 1) for every one.
  4d. Verify a genuine receipt against a key set with an unknown kid ->
      UNDETERMINED (exit 2), NOT INVALID: an outage must never read as fraud.
"""

import json
import subprocess
import sys
import attacklib as A

import os
import tempfile

# Derive the repo root from this file's location (scripts/invariant_attacks/…)
# so the harness runs on any machine, not just the one it was authored on.
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WORK = os.path.join(tempfile.gettempdir(), "atk4_forgery")
os.makedirs(WORK, exist_ok=True)


def verify(bundle_path, keys_path):
    # Verify with the running interpreter; the SDK verifier imports no server
    # code and only needs b2a_sdk/src on PYTHONPATH.
    p = subprocess.run(
        [
            sys.executable,
            "-m",
            "b2a_sdk.verify_cli",
            "--bundle",
            bundle_path,
            "--keys",
            keys_path,
        ],
        cwd=ROOT,
        env={
            **os.environ,
            "PYTHONPATH": os.path.join(ROOT, "..", "sdk", "python", "src"),
        },
        capture_output=True,
        text=True,
    )
    return {
        "exit": p.returncode,
        "stdout": p.stdout.strip(),
        "stderr": p.stderr.strip(),
    }


def fetch_bundle(cred, receipt_id, name):
    r = A.http("GET", f"/v1/receipts/{receipt_id}/portable", api_key=cred["api_key"])
    path = f"{WORK}/{name}.json"
    with open(path, "w") as fh:
        json.dump(r["json"], fh)
    return path, r["json"]


def main():
    cred = A.provision("atk4-forgery")
    permit = A.issue_permit(
        cred,
        allowed_tools=["partner.notes.write"],
        scopes=["tool:partner.notes.write:invoke", "billing:charge"],
        max_credits=100,
        idem="atk4-permit",
    )["json"]
    # success receipt
    inv = A.invoke(
        cred["api_key"],
        cred["wallet_id"],
        permit["permit_id"],
        "atk4-ok",
        "atk4 good note",
    )
    success_rid = A.receipt_of(inv)["receipt_id"]
    # denial receipt (scope escape)
    deny_permit = A.issue_permit(
        cred,
        allowed_tools=["some.other.tool"],
        scopes=["tool:some.other.tool:invoke", "billing:charge"],
        max_credits=100,
        idem="atk4-denypermit",
    )["json"]
    deny = A.http(
        "POST",
        "/mcp/messages",
        api_key=cred["api_key"],
        body=A.invoke_body(
            cred["wallet_id"], deny_permit["permit_id"], "atk4-deny", "denied note"
        ),
    )
    denial_rid = A.receipt_of(deny)["receipt_id"]

    # keys
    keys = A.http("GET", "/.well-known/trust-keys.json")
    keys_path = f"{WORK}/keys.json"
    with open(keys_path, "w") as fh:
        json.dump(keys["json"], fh)

    good_path, good_bundle = fetch_bundle(cred, success_rid, "good")
    denial_path, _ = fetch_bundle(cred, denial_rid, "denial")

    results = {}
    results["4a_success_genuine"] = verify(good_path, keys_path)
    results["4b_denial_genuine"] = verify(denial_path, keys_path)

    signing_input = good_bundle["signing_input"]
    # 4c: mutate representative high-value signed facts in signing_input
    # (string surgery, re-sign nothing). The shared helper deliberately includes
    # ledger_entry_id so the harness covers the receipt-to-ledger binding it
    # claims to protect.
    tampers = A.signed_receipt_tamper_cases(
        good_bundle, fallback_wallet_id=cred["wallet_id"]
    )
    forge_results = {}
    for name, (old, new) in tampers.items():
        if old not in signing_input:
            forge_results[name] = {"skipped": f"pattern not present: {old!r}"}
            continue
        forged = dict(good_bundle)
        forged["signing_input"] = signing_input.replace(old, new, 1)
        fpath = f"{WORK}/forged_{name}.json"
        with open(fpath, "w") as fh:
            json.dump(forged, fh)
        forge_results[name] = verify(fpath, keys_path)
    results["4c_tampered_facts"] = forge_results

    # 4d: unknown key (key-server outage / rotated-away kid). Rename every kid so
    # the receipt's signing kid cannot be resolved. A correct verifier must report
    # UNDETERMINED (exit 2), never INVALID (exit 1): an outage is not fraud.
    kdoc = json.loads(open(keys_path).read())
    klist = kdoc.get("keys") or kdoc.get("trust_keys") or []
    mutated = json.loads(json.dumps(kdoc))
    mlist = mutated.get("keys") or mutated.get("trust_keys") or []
    for k in mlist:
        if "kid" in k:
            k["kid"] = k["kid"] + "-ROTATED-AWAY"
        if isinstance(k.get("jwk"), dict) and "kid" in k["jwk"]:
            k["jwk"]["kid"] = k["jwk"]["kid"] + "-ROTATED-AWAY"
    mkeys_path = f"{WORK}/keys_unknown_kid.json"
    with open(mkeys_path, "w") as fh:
        json.dump(mutated, fh)
    results["4d_genuine_vs_unknown_key"] = verify(good_path, mkeys_path)
    results["_key_fields_seen"] = list(klist[0].keys()) if klist else []

    checks = {
        "success_verifies": results["4a_success_genuine"]["exit"] == 0,
        "denial_verifies": results["4b_denial_genuine"]["exit"] == 0,
        # Fail closed: every tamper case must have RUN and been rejected. A
        # skipped case (pattern absent from signing_input) means that forgery
        # variant was never exercised, so it must not count toward HELD — and an
        # all-skipped set must not pass via all([]) == True.
        "all_tampers_rejected": len(forge_results) == len(tampers)
        and all(v.get("exit") == 1 for v in forge_results.values()),
        "unknown_key_undetermined_not_invalid": results["4d_genuine_vs_unknown_key"][
            "exit"
        ]
        == 2,
    }
    verdict = "HELD" if all(checks.values()) else "BROKE"
    ev = {
        "attack": "4 - forged receipts",
        "verdict": verdict,
        "checks": checks,
        "success_receipt_id": success_rid,
        "denial_receipt_id": denial_rid,
        "results": results,
    }
    print(json.dumps(ev, indent=2, default=str))
    with open("evidence_attack4.json", "w") as fh:
        json.dump(ev, fh, indent=2, default=str)
    return A.verdict_exit_code(verdict)


if __name__ == "__main__":
    sys.exit(main())
