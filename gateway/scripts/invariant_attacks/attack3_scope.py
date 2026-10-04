"""ATTACK 3 — Scope escape.

Invariant under test: a permit authorizes only the tools it names. A call for a
tool outside the permit's allowed_tools must be denied with a signed denial
receipt, no charge, and no side effect.

Sub-tests:
  3a. Permit allows ONLY `some.other.tool`; invoke `partner.notes.write`.
      Expect: `permit_tool_not_allowed`, signed denial receipt, credits=0,
      ledger_entry_id=null, no note written.
  3b. Permit with EMPTY allowed_tools; invoke `partner.notes.write`. Expect denial.
  3c. Invoke a tool that is not registered at all. Expect denial/no charge.
"""

import json
import sys
import attacklib as A

MARK = "atk3-scope"


def one_case(cred, allowed_tools, scopes, tool_name, idem_key, label):
    permit = A.issue_permit(
        cred,
        allowed_tools=allowed_tools,
        scopes=scopes,
        max_credits=100,
        idem=f"atk3-permit-{label}",
    )
    pj = permit.get("json") or {}
    pid = pj.get("permit_id")
    body = {
        "jsonrpc": "2.0",
        "id": f"atk3-{label}",
        "method": "tools/call",
        "params": {
            "name": tool_name,
            "arguments": {"text": f"{MARK} {label}"},
            "mcpContext": {
                "wallet_id": cred["wallet_id"],
                "permit_id": pid,
                "idempotency_key": idem_key,
            },
        },
    }
    notes_before = A.notes_count_for(MARK)
    resp = A.http("POST", "/mcp/messages", api_key=cred["api_key"], body=body)
    notes_after = A.notes_count_for(MARK)
    rc = A.receipt_of(resp)
    led = A.ledger(cred)["json"]
    return {
        "label": label,
        "permit_allowed_tools": allowed_tools,
        "invoked_tool": tool_name,
        "http": resp["status"],
        "reason": A.reason_of(resp),
        "outcome": A.outcome_of(resp),
        "signed_denial_receipt": bool(rc),
        "credits_charged": rc.get("credits_charged") if rc else None,
        "ledger_entry_id": rc.get("ledger_entry_id") if rc else None,
        "receipt_id": rc.get("receipt_id") if rc else None,
        "signature_present": bool(rc.get("signature")) if rc else False,
        "notes_written": notes_after - notes_before,
        "period_debits": led["period_debits_exact"],
        "raw": resp.get("json"),
    }


def main():
    cred = A.provision("atk3-scope-escape")
    a = one_case(
        cred,
        ["some.other.tool"],
        ["tool:some.other.tool:invoke", "billing:charge"],
        "partner.notes.write",
        "atk3-a",
        "a_tool_not_allowed",
    )
    b = one_case(
        cred, [], ["billing:charge"], "partner.notes.write", "atk3-b", "b_empty_allowed"
    )
    c = one_case(
        cred,
        ["partner.notes.write"],
        ["tool:partner.notes.write:invoke", "billing:charge"],
        "admin.delete.everything",
        "atk3-c",
        "c_unregistered_tool",
    )

    # Invariant: every case denied, zero charge, zero side effect
    denied = all(x["outcome"] != "success" for x in (a, b, c))
    no_charge = (
        float(a["period_debits"]) == 0.0
        and float(b["period_debits"]) == 0.0
        and float(c["period_debits"]) == 0.0
    )
    no_side_effect = all(x["notes_written"] == 0 for x in (a, b, c))
    a_signed = (
        a["signed_denial_receipt"]
        and a["reason"] == "permit_tool_not_allowed"
        and str(a["credits_charged"]) in ("0", "0.00000000", "0.0")
        and a["ledger_entry_id"] in (None, "null")
    )

    verdict = (
        "HELD" if (denied and no_charge and no_side_effect and a_signed) else "BROKE"
    )
    ev = {
        "attack": "3 - scope escape",
        "verdict": verdict,
        "checks": {
            "all_denied": denied,
            "no_charge": no_charge,
            "no_side_effect": no_side_effect,
            "primary_signed_denial": a_signed,
        },
        "cases": {
            "a_tool_not_allowed": a,
            "b_empty_allowed": b,
            "c_unregistered_tool": c,
        },
    }
    print(json.dumps(ev, indent=2, default=str))
    with open("evidence_attack3.json", "w") as fh:
        json.dump(ev, fh, indent=2, default=str)
    return A.verdict_exit_code(verdict)


if __name__ == "__main__":
    sys.exit(main())
