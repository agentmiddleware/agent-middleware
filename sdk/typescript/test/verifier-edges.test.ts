import { test } from "node:test";
import assert from "node:assert/strict";
import { verifyBundle } from "../src/verify.ts";

function bundle(signingInput: string, signature = Buffer.alloc(64).toString("base64")) {
  return {
    signing_input: signingInput,
    signature,
    kid: "k1",
    alg: "Ed25519",
    canonicalization: "awi-canonical-json/1",
  };
}

test("a lone surrogate is malformed, not a bad signature", () => {
  // Python rejects this before it looks at the signature. A bad signature
  // must not turn "cannot read the text" into "tampered".
  const signing = '{"kid":"k1","receipt_id":"r1","note":"\\ud800"}';
  const result = verifyBundle(bundle(signing), new Map([["k1", Buffer.alloc(32, 1)]]));
  assert.equal(result.status, "malformed");
  assert.equal(result.ok, false);
});

test("a public key that is not 32 bytes is malformed, not tampered", () => {
  const signing = '{"kid":"k1","receipt_id":"r1"}';
  const result = verifyBundle(bundle(signing), new Map([["k1", Buffer.alloc(31, 2)]]));
  assert.equal(result.status, "malformed");
  assert.equal(result.ok, false);
});
