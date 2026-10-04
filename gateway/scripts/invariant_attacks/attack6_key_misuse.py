"""ATTACK 6 — Key / credential misuse.

Invariants under test: a credential only acts within its own authority.
  6a. Garbage / no API key -> rejected on a governed invoke; no side effect.
  6b. Revoked key -> rejected after DELETE /v1/api-keys/{wallet}/{key_id}.
  6c. Confused deputy: permit bound to key A's subject_key_id, invoked by key B
      (different wallet+key) -> permit_key_mismatch / access denied; no charge.
  6d. Cross-tenant: wallet-scoped key A cannot issue a permit for wallet B it
      does not own (403), and cannot read wallet B's ledger (403).
  6e. Privilege escalation: wallet-scoped key cannot read the admin audit plane.
"""

import json
import sys
import attacklib as A

MARK = "atk6"


def matches_response(resp, *, status, reason=None):
    """Require the exact denial contract; outages and 5xxs are not denials."""
    return resp.get("status") == status and (
        reason is None or A.reason_of(resp) == reason
    )


def main():
    A_cred = A.provision("atk6-alice")
    B_cred = A.provision("atk6-bob")

    # A permit legitimately owned/bound by A
    permitA = A.issue_permit(
        A_cred,
        allowed_tools=["partner.notes.write"],
        scopes=["tool:partner.notes.write:invoke", "billing:charge"],
        max_credits=100,
        idem="atk6-permitA",
    )["json"]
    pidA = permitA["permit_id"]

    notes0 = A.notes_count_for(MARK)

    # 6a: garbage key and missing key on a governed invoke. The bogus value is a
    # low-entropy, obviously-fake placeholder (no real credential) built at
    # runtime so it is not a hardcoded-credential literal.
    bogus_key = "-".join(["not", "a", "valid", "key"])
    body = A.invoke_body(A_cred["wallet_id"], pidA, "atk6-garbage", f"{MARK} garbage")
    garbage = A.http("POST", "/mcp/messages", api_key=bogus_key, body=body)
    missing = A.http("POST", "/mcp/messages", api_key=None, body=body)

    # 6c: confused deputy — B invokes using A's permit (bound to A's key), claiming A's wallet
    deputy_awallet = A.http(
        "POST",
        "/mcp/messages",
        api_key=B_cred["api_key"],
        body=A.invoke_body(
            A_cred["wallet_id"], pidA, "atk6-deputy1", f"{MARK} deputy-awallet"
        ),
    )
    # ...and B claiming its own wallet but using A's permit id
    deputy_bwallet = A.http(
        "POST",
        "/mcp/messages",
        api_key=B_cred["api_key"],
        body=A.invoke_body(
            B_cred["wallet_id"], pidA, "atk6-deputy2", f"{MARK} deputy-bwallet"
        ),
    )

    # 6d: cross-tenant permit issuance and ledger read
    xtenant_permit = A.issue_permit(
        A_cred,
        allowed_tools=["partner.notes.write"],
        scopes=["tool:partner.notes.write:invoke", "billing:charge"],
        max_credits=100,
        idem="atk6-xtenant",
        subject_wallet_id=B_cred["wallet_id"],
    )  # B's wallet, A's key
    xtenant_ledger = A.http(
        "GET", f"/v1/billing/ledger/{B_cred['wallet_id']}", api_key=A_cred["api_key"]
    )

    # 6e: tenant isolation on the audit plane — a wallet-scoped key must see ONLY
    # its own wallet's audit slice, never other tenants' events or the global plane.
    # (The endpoint returns 200 but self-scopes non-admins: app/routers/audit.py:148.)
    # Make B generate a distinct audit event first, then read A's summary.
    A.http(
        "POST",
        "/mcp/messages",
        api_key=B_cred["api_key"],
        body=A.invoke_body(
            B_cred["wallet_id"],
            "permit-does-not-exist",
            "atk6-bnoise",
            f"{MARK} b-noise",
        ),
    )
    audit = A.http("GET", "/v1/audit/summary", api_key=A_cred["api_key"])
    audit_wallets = list(((audit.get("json") or {}).get("by_wallet") or {}).keys())
    audit_self_scoped = audit["status"] == 200 and set(audit_wallets) == {
        A_cred["wallet_id"]
    }
    audit_b = A.http("GET", "/v1/audit/summary", api_key=B_cred["api_key"])
    b_wallets = list(((audit_b.get("json") or {}).get("by_wallet") or {}).keys())
    audit_b_self_scoped = audit_b["status"] == 200 and set(b_wallets) == {
        B_cred["wallet_id"]
    }

    # 6b: revoke A's own key, then try to use it
    revoke = A.http(
        "DELETE",
        f"/v1/api-keys/{A_cred['wallet_id']}/{A_cred['key_id']}",
        api_key=A_cred["api_key"],
    )
    after_revoke = A.http(
        "POST",
        "/mcp/messages",
        api_key=A_cred["api_key"],
        body=A.invoke_body(
            A_cred["wallet_id"], pidA, "atk6-postrevoke", f"{MARK} post-revoke"
        ),
    )

    notes1 = A.notes_count_for(MARK)

    cases = {
        "6a_garbage_key": {
            "http": garbage["status"],
            "reason": A.reason_of(garbage),
            "raw": garbage.get("json"),
        },
        "6a_missing_key": {
            "http": missing["status"],
            "reason": A.reason_of(missing),
            "raw": missing.get("json"),
        },
        "6c_deputy_A_wallet": {
            "http": deputy_awallet["status"],
            "reason": A.reason_of(deputy_awallet),
            "outcome": A.outcome_of(deputy_awallet),
            "raw": deputy_awallet.get("json"),
        },
        "6c_deputy_B_wallet": {
            "http": deputy_bwallet["status"],
            "reason": A.reason_of(deputy_bwallet),
            "outcome": A.outcome_of(deputy_bwallet),
            "raw": deputy_bwallet.get("json"),
        },
        "6d_xtenant_permit": {
            "http": xtenant_permit["status"],
            "reason": A.reason_of(xtenant_permit),
            "raw": xtenant_permit.get("json"),
        },
        "6d_xtenant_ledger": {
            "http": xtenant_ledger["status"],
            "reason": A.reason_of(xtenant_ledger),
            "raw": xtenant_ledger.get("json"),
        },
        "6e_audit_self_scoped": {
            "http": audit["status"],
            "A_wallet": A_cred["wallet_id"],
            "by_wallet_keys_seen_by_A": audit_wallets,
            "by_wallet_keys_seen_by_B": b_wallets,
            "raw": audit.get("json"),
        },
        "6b_revoke_call": {
            "http": revoke["status"],
            "raw": revoke.get("json") or revoke.get("text"),
        },
        "6b_post_revoke_invoke": {
            "http": after_revoke["status"],
            "reason": A.reason_of(after_revoke),
            "outcome": A.outcome_of(after_revoke),
            "raw": after_revoke.get("json"),
        },
    }

    checks = {
        "garbage_denied_exactly": matches_response(
            garbage, status=403, reason="invalid_api_key"
        ),
        "missing_denied_exactly": matches_response(
            missing, status=401, reason="missing_credentials"
        ),
        "deputy_A_wallet_denied_exactly": matches_response(
            deputy_awallet, status=200, reason="wallet_access_denied"
        ),
        "deputy_B_wallet_denied_exactly": matches_response(
            deputy_bwallet, status=200, reason="permit_wallet_mismatch"
        ),
        "xtenant_permit_denied_exactly": matches_response(
            xtenant_permit, status=403, reason="subject_wallet_access_denied"
        ),
        "xtenant_ledger_denied_exactly": matches_response(
            xtenant_ledger, status=403, reason="wallet_access_denied"
        ),
        "audit_A_self_scoped_no_cross_tenant": audit_self_scoped,
        "audit_B_self_scoped_no_cross_tenant": audit_b_self_scoped,
        "revocation_succeeded": matches_response(revoke, status=204),
        "revoked_key_denied_exactly": matches_response(
            after_revoke, status=403, reason="invalid_api_key"
        ),
        "no_side_effect": (notes1 - notes0) == 0,
    }
    verdict = "HELD" if all(checks.values()) else "BROKE"
    ev = {
        "attack": "6 - key misuse",
        "verdict": verdict,
        "checks": checks,
        "notes_written_total": notes1 - notes0,
        "cases": cases,
        "A_key": A.redact(A_cred["api_key"]),
        "B_key": A.redact(B_cred["api_key"]),
    }
    print(json.dumps(ev, indent=2, default=str))
    with open("evidence_attack6.json", "w") as fh:
        json.dump(ev, fh, indent=2, default=str)
    return 0 if verdict == "HELD" else 1


if __name__ == "__main__":
    sys.exit(main())
