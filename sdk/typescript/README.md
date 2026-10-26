# Agent Middleware (AMW) TypeScript receipt verifier

Apache 2.0 (gateway is Fair Source, FSL-1.1)

Package: `@agent-middleware/verify`. Status: not published to npm.

Offline verification of Agent Middleware receipt bundles, using Node's built-in
Ed25519 support. No runtime dependencies and no network access.

```ts
import { keySetFromDocument, verifyBundle } from "@agent-middleware/verify";

const keys = keySetFromDocument(JSON.parse(trustKeysJson)); // /.well-known/trust-keys.json
const result = verifyBundle(JSON.parse(bundleJson), keys);
// A JSON string or its UTF-8 bytes are accepted too: verifyBundle(bundleJson, keys)
if (result.ok) console.log(result.claims?.tool, result.claims?.credits_charged);
else console.log(result.status, result.reason); // invalid | mismatch | unknown_key | malformed | unsupported
```

From a checkout, build first (`npm run build` emits `dist/`) or import
the source directly (`./src/verify.ts`); both expose the same surface.

Semantics match the Python verifier in `../python` and the format in
[`../../spec/receipt-v0.1.md`](../../spec/receipt-v0.1.md).

```bash
npm install       # dev dependencies only (typescript, @types/node)
npm run build     # emits dist/verify.js plus types; what a consumer imports
npm run lint      # checks types and unused code
npm test          # builds, then runs verifier and package export tests (Node 22.18+ runs .ts directly)
npm run typecheck # optional
```

A TypeScript client for the gateway API is not written yet.

For shell verification, install the Python SDK with its `verify` extra and run:

```bash
amw verify --bundle receipt.json --keys trust-keys.json
```
