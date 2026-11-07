# Interop statement: permits, receipts, and keys (v0.1, draft)

One page for an evaluator who wants to check our homework without trusting
our sales copy.

## What is stable and what is not

The format is **draft v0.1**. What that means in practice:

- Additive fields may appear (a new optional member on a permit or receipt).
  Old bundles keep verifying, because verifiers ignore members they do not
  know and the signature covers whatever is present.
- Anything that changes what a verifier must do (a new algorithm, a new
  canonicalization, a removed or redefined member) bumps the version and is
  proposed as a spec RFC first (see `CONTRIBUTING.md`).
- Do not treat v0.1 field names as frozen. Do treat the verification rules
  (sorted keys, no whitespace, `kid` inside the signed payload,
  `payload_hash`) as the contract: they are pinned by the shared vectors.

## Where the live key set lives

A running gateway publishes its verification keys at

```
GET /.well-known/trust-keys.json
```

No credential is needed. The route is mounted in every deployment mode. When
no usable key is published yet, it answers 503 with `trust_keys_unavailable`,
which a verifier must read as "cannot tell yet", never as fraud. The same
key material is also served as standard JWKS at `/.well-known/jwks.json`.

## How to verify without us

Two independent offline verifiers share one vector suite and need no
account, no network call to the issuer, and no gateway code:

| Language   | Receipts | Permits |
|---|---|---|
| Python (`sdk/python`) | `b2a_sdk.receipt_verifier.verify_bundle` | `b2a_sdk.receipt_verifier.verify_permit_bundle` |
| TypeScript (`sdk/typescript`) | `verifyBundle` in `src/verify.ts` | `verifyPermitBundle` in `src/verify.ts` |

Run the shared suite:

```bash
python verifier/check_vectors.py          # Python reference verifier
(cd sdk/typescript && npm test)           # TypeScript verifier
```

`spec/test-vectors/` holds the cases: signed receipt bundles plus tampered
variants (`receipts.json`), signed permit bundles plus tampered variants
(`permits.json`), and a key set with a rotation successor and a disabled key
(`keys.json`). Every verifier status (`verified`, `invalid`, `mismatch`,
`unknown_key`, `malformed`, `unsupported`) has at least one exercising case
in each suite. Regenerate them with `python spec/test-vectors/generate.py`;
the generator re-signs the gateway's golden fixtures, so gateway drift shows
up as a diff.

## What a verified signature proves, and what it does not

A verified signature proves the holder of the named key signed those bytes.
It does not prove a downstream side effect happened, or happened only once.
Two limits matter for an audit:

1. **Revocation is live-checked.** An exported permit or receipt still
   verifies offline after revocation. Recheck against the server before
   honoring a permit, and say so in the audit file.
2. **Trust in the key set is yours.** Fetching keys from the same origin you
   are auditing bootstraps trust, it does not establish it. Pin the key set
   through a channel you already trust for anything that matters.
