# Offline receipt verifier

License: Apache-2.0.

A receipt is only "verifiable without us" if anyone can check it with no account,
no network call to the issuer, and no gateway code. Two independent
implementations do that:

| Language | Receipts | Permits |
|---|---|---|
| Python ([`../sdk/python/`](../sdk/python/)) | `b2a_sdk.receipt_verifier.verify_bundle` (`b2a-verify-receipt` CLI) | `b2a_sdk.receipt_verifier.verify_permit_bundle` |
| TypeScript ([`../sdk/typescript/`](../sdk/typescript/)) | `verifyBundle` in `src/verify.ts` | `verifyPermitBundle` in `src/verify.ts` |

Neither imports the gateway. Both pass the shared vectors in
[`../spec/test-vectors/`](../spec/test-vectors/) (`receipts.json` and
`permits.json`).

```bash
# Python, from the repository root
pip install -e "./sdk/python[verify]"
b2a-verify-receipt --bundle receipt.json --keys trust-keys.json
python verifier/check_vectors.py

# TypeScript (Node 22.18+)
cd sdk/typescript && npm test
```

Exit codes of the CLI: `0` verified, `1` rejected (signature failed or the
bundle contradicts its signed bytes), `2` cannot tell (unknown key, malformed
input). Treat `2` as "go get the key set", not as fraud.

What verification proves and does not prove: [`../spec/README.md`](../spec/README.md).
