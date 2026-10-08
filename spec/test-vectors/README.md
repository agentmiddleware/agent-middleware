# Test vectors

- `keys.json`: key set holding the public halves of the fixed test seeds.
  **Test use only.** It carries two active keys (the original plus its
  rotation successor) and one `disabled` key, so rotation and revocation
  handling are exercised, not just described.
- `receipts.json`: receipt cases with a `bundle`, optional `keys` override,
  and the `expect`ed verifier status. Built from the gateway's golden receipt
  fixtures plus tampered variants, a rotation case, a disabled-key case, and
  future-algorithm and future-canonicalization cases.
- `permits.json`: the same treatment for permits, built from the gateway's
  permit golden fixtures (`gateway/tests/fixtures/permit_signing_golden`)
  plus tampered variants and a future-algorithm case.
- `generate.py`: regenerates all three files from `gateway/tests/fixtures/`.
  It asserts every re-signed payload still matches its golden fixture, and
  self-verifies each derived signature before writing it.

Every verifier status (`verified`, `invalid`, `mismatch`, `unknown_key`,
`malformed`, `unsupported`) has at least one exercising case in each of
`receipts.json` and `permits.json`.

Check an implementation:

```bash
python verifier/check_vectors.py          # Python reference verifier
(cd sdk/typescript && npm test)           # TypeScript verifier
```
