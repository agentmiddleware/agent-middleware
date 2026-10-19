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

- A verifier uses `public_key_b64` when it decodes to 32 bytes. It uses the
  `jwk` mirror only when that raw form is missing or unusable. Publish both
  only when they are the same key: a usable `public_key_b64` wins when they
  disagree, and the signature check then fails closed against the raw key.
- Keys with `status` `active` or `retired` are verification keys. `retired`
  means this key no longer signs new objects, and objects it already signed
  still verify. Keys with `status` `disabled` must be dropped before
  verification. A cached copy of the document must not keep a disabled key
  working.
- Rotation: publish the new key, switch signing to it, and set the previous
  key to `retired` for as long as its receipts must stay verifiable. Set it
  to `disabled` when those receipts should stop verifying.
- Extra members such as `created_at`, `schema_version`, and `issuer` may
  appear on the document or on a key. Verifiers ignore members they do not use.
- Trust in the key set (which origin you fetched it from, and whether you
  pinned it) is the verifier's decision, not something the receipt proves.

Test key set: [`test-vectors/keys.json`](test-vectors/keys.json) (derived from
a fixed public test seed; never use it for anything real).
