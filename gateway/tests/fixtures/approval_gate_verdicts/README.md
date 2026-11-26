# Signed approval-gate verdict fixtures

This test-only pair lets reviewers inspect the external verdict shape proposed
in [issue #341](https://github.com/agentmiddleware/agent-middleware/issues/341).
The JSON comes from
[the pinned upstream directory](https://github.com/babyblueviper1/preaction-governance-conformance/tree/de7c8fc377a2677e186a6a2f4eb5ec5d732a3d70/examples/amw-approval-gate-v0)
at commit `de7c8fc377a2677e186a6a2f4eb5ec5d732a3d70`. Only a final newline
has been added to each JSON file. The canonical calls, signed event content,
public keys and signatures are unchanged. No private keys are included.

| Fixture | Signed call | Verdict | Checker result |
| --- | --- | --- | --- |
| `approve.json` | First 5000-cent refund of `ch_test_amw_fixture_0001` | `approve` | `ALLOW` |
| `reject.json` | Retry with a fresh idempotency key and a stated prior full refund | `reject` | `BLOCK` |

These are the published signed verdicts for test charge IDs. The checker never
dispatches a tool, contacts Stripe, or calls an external verifier.

Run from the repository root with Python 3.11 or newer, using only the stdlib:

```bash
python3 gateway/tests/fixtures/approval_gate_verdicts/check.py \
  gateway/tests/fixtures/approval_gate_verdicts/approve.json \
  gateway/tests/fixtures/approval_gate_verdicts/reject.json
```

Expected output is `ALLOW` for approve and `BLOCK` for reject, with each signed
event ID printed as the receipt digest. Exit status is 1 if any input blocks
(including the expected reject above), otherwise 0. Use `--call path/to/call.json`
to inspect a verdict against another call. Identical arguments produce the same
decision and digest; changing the amount or idempotency key blocks the approve.

The checker recomputes the NIP-01 event ID, verifies the BIP-340 signature against
the pinned public key, and compares SHA-256 of the supplied canonical call with
`artifact_hash` inside signed `proof.event.content`. Only a valid, bound
`approve` allows. Invalid or malformed inputs block without a receipt digest.
The unsigned `proof.proof_payload` and outer `artifact_sha256` are informational;
they cannot override the signed verdict. The companion ML-DSA signature is
preserved but is not checked. The `--call` serializer matches these fixtures'
ASCII string/integer JSON calls; it is not a general RFC 8785 implementation.
The checker does not assess freshness, caller assertions, or refund safety.

AMW's current [human approval flow](../../../docs/human-approval-gate.md) and
[`ApprovalGate` tests](../../test_approval_gate.py) use their existing approval
mechanisms. This fixture checker does not authorize runtime invocations.
[`test_approval_gate_verdicts.py`](../../test_approval_gate_verdicts.py) exercises
the pair, tampering, exact-call binding, and retries. It also creates a real
zero-charge denial receipt through `ReceiptService`, places the reject event ID
in the existing `approval_digest` field, and verifies that changing that field
invalidates the receipt signature. No Stripe execution is represented by that test.

The upstream fixtures and helper code are MIT licensed; the original notice is
in [LICENSE](LICENSE). `check.py` adapts upstream `check.py` with strict event
shape checks, malformed-input handling, and a blocking exit status.
`_bip340_nostr.py` retains only verification math and event hashing from the
[upstream helper](https://github.com/babyblueviper1/preaction-governance-conformance/blob/de7c8fc377a2677e186a6a2f4eb5ec5d732a3d70/_bip340_nostr.py),
with local documentation and formatting. Gateway contributions otherwise remain
under FSL-1.1-ALv2 (Fair Source).
