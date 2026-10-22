# Test vectors

- `keys.json`: key set holding the public half of the fixed test seed
  `bytes(range(32))`. **Test use only.**
- `receipts.json`: cases with a `bundle`, optional `keys` override, and the
  `expect`ed verifier status. Built from the gateway's golden receipt fixtures
  plus tampered variants. Synthetic cases add retired and disabled keys,
  envelope fields that disagree with the signed payload, an advisory
  payload_hash that does not match recomputation (still verified when
  signed), a float-bearing payload that verifies without re-serialization,
  a genuine P-256 signature classified as unsupported, and malformed bundles.
- `generate.py`: regenerates both files from `gateway/tests/fixtures/`.

Check an implementation:

```bash
python verifier/check_vectors.py          # Python reference verifier
(cd sdk/typescript && npm test)           # TypeScript verifier
```
