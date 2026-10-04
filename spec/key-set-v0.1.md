# Key set format v0.1 (draft)

A gateway publishes its receipt and permit verification keys at
`/.well-known/trust-keys.json`. Fetching it needs no credential.

```json
{
  "keys": [
    {
      "kid": "sk_...",
      "alg": "Ed25519",
      "status": "active",
      "public_key_b64": "<32-byte raw public key, base64>",
      "jwk": {"kty": "OKP", "crv": "Ed25519", "x": "<base64url, no padding>"}
    }
  ]
}
```

- A verifier accepts either `public_key_b64` or the `jwk` mirror.
- `status: "disabled"` keys must be dropped before verification; a cached copy
  of the document must not keep a revoked key working.
- Rotation: publish the new key, switch signing to it, keep the old key
  `active` for as long as its receipts must stay verifiable, then disable it.
- Trust in the key set (which origin you fetched it from, and whether you
  pinned it) is the verifier's decision, not something the receipt proves.

Test key set: [`test-vectors/keys.json`](test-vectors/keys.json) (derived from
a fixed public test seed; never use it for anything real).
