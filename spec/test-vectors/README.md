# Test vectors

- `keys.json`: key set holding the public half of the fixed test seed
  `bytes(range(32))`. **Test use only.**
- `receipts.json`: cases with a `bundle`, optional `keys` override, and the
  `expect`ed verifier status. Built from the gateway's golden receipt fixtures
  plus tampered variants, retired and disabled keys, and envelope fields that
  disagree with the signed payload.
- `generate.py`: regenerates both files from `gateway/tests/fixtures/`.

Check an implementation:

```bash
python verifier/check_vectors.py          # Python reference verifier
(cd sdk/typescript && npm test)           # TypeScript verifier
```
