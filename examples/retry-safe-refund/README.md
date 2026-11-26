# Retry safe payout sandbox demo

Run this command from the repository root:

```bash
bash examples/retry-safe-refund/run.sh
```

The payout provider is a local memory stub. No money moves and the demo makes no provider network request. The gateway completes a payout, the demo drops its response, and a retry with the same key returns the original receipt instead of dispatching again. The script asserts one provider dispatch, the same receipt and outcome on retry, and rejection of a changed payout under the same key.

The script saves the signed receipt and public keys in `examples/retry-safe-refund/output/`. Verify them offline with the existing SDK from the repository root:

```bash
(cd gateway && UV_CACHE_DIR=../examples/retry-safe-refund/.uv-cache \
  uv run --offline --with-requirements requirements.txt --with "../sdk/python[verify]" \
  python -m b2a_sdk.verify_cli \
  --bundle ../examples/retry-safe-refund/output/receipt-bundle.json \
  --keys ../examples/retry-safe-refund/output/trust-keys.json \
  --expect-issuer http://sandbox)
```

The public keys come from this local sandbox run. They prove the receipt matches this run's signer, not the identity of any external provider.
