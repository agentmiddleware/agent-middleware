# @agent-middleware/verify (TypeScript)

License: Apache-2.0. Status: source only, not published to npm.

Offline verification of Agent Middleware receipt bundles, using Node's built-in
Ed25519 support. No runtime dependencies and no network access.

```ts
import { keySetFromDocument, verifyBundle } from "./src/verify.ts";

const keys = keySetFromDocument(JSON.parse(trustKeysJson)); // /.well-known/trust-keys.json
const result = verifyBundle(JSON.parse(bundleJson), keys);
if (result.ok) console.log(result.claims?.tool, result.claims?.credits_charged);
else console.log(result.status, result.reason); // invalid | mismatch | unknown_key | malformed | unsupported
```

Semantics match the Python verifier in `../python` and the format in
[`../../spec/receipt-v0.1.md`](../../spec/receipt-v0.1.md).

```bash
npm test            # runs the shared spec/test-vectors (Node 22.18+ runs .ts directly)
npm run typecheck   # optional, needs `npm install`
```

A TypeScript client for the gateway API is not written yet.
