# Test vectors

- `keys.json`: public half of the fixed receipt and permit test seed. Test use only.
- `receipts.json`: receipt bundles from the gateway's golden fixtures, their
  tampered variants, and the existing lifecycle, envelope, advisory hash,
  float, P-256, and malformed cases.
- `rotation.json`: the existing rotated receipt case, with both active public
  keys supplied for that case. The default receipt corpus keeps its single-key
  shape used by the gateway fixture tests.
- `permits.json`: permit bundles from the gateway's golden fixtures, their
  tampered variants, and an unsupported algorithm case.
- `generate.py`: regenerates the JSON files from the golden fixtures. It checks
  fixture signatures before writing any file.

The receipt and permit suites each exercise `verified`, `invalid`, `mismatch`,
`unknown_key`, `malformed`, and `unsupported`.

Check an implementation:

```bash
python verifier/check_vectors.py          # Python reference verifier
(cd sdk/typescript && npm test)           # TypeScript verifier
```
