---
title: TS verifier
section: Reference
order: 4
description: Verify a draft receipt bundle offline with the source TypeScript package.
---

# TS verifier

The [TypeScript verifier source](https://github.com/agentmiddleware/agent-middleware/tree/main/sdk/typescript) checks a portable Receipt against a supplied key set without making a network request. Its `@agent-middleware/verify` package is not published to npm. Use Node 22.18 or newer and build it from the canonical checkout:

```bash
git clone https://github.com/agentmiddleware/agent-middleware
cd agent-middleware/sdk/typescript
npm install
npm run build
```

Place an exported `receipt.json` and its matching `trust-keys.json` in that directory, then run:

```bash
node --input-type=module <<'JS'
import { readFileSync } from 'node:fs';
import { keySetFromDocument, verifyBundle } from './dist/verify.js';

const bundle = JSON.parse(readFileSync('receipt.json', 'utf8'));
const keys = keySetFromDocument(JSON.parse(readFileSync('trust-keys.json', 'utf8')));
const result = verifyBundle(bundle, keys);
console.log(result.status, result.reason ?? '');
if (result.ok) console.log(result.claims.tool);
JS
```

`verified` means the signature checked under a supplied key. `invalid` and `mismatch` reject a bundle. `unknown_key`, `malformed`, and `unsupported` mean the verifier could not establish a valid Receipt. This source verifier covers the draft plain Ed25519 format; it does not handle the gateway's tagged KMS envelope signature. Use the [Python verifier](/docs/python-sdk/) for that form. See the [draft Receipt spec](https://github.com/agentmiddleware/agent-middleware/blob/main/spec/receipt-v0.1.md).
