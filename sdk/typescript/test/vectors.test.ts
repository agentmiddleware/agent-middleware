import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { canonicalJson, keySetFromDocument, verifyBundle } from "../src/verify.ts";

const vectors = join(dirname(fileURLToPath(import.meta.url)), "..", "..", "..", "spec", "test-vectors");
const keys = keySetFromDocument(JSON.parse(readFileSync(join(vectors, "keys.json"), "utf8")));
const { cases } = JSON.parse(readFileSync(join(vectors, "receipts.json"), "utf8"));

for (const c of cases) {
  test(c.name, () => {
    const ks = c.keys ? keySetFromDocument(c.keys) : keys;
    assert.equal(verifyBundle(c.bundle, ks).status, c.expect);
  });
}

test("float-bearing vector keeps the signed integer above 2^53", () => {
  const floatCase = cases.find((c: { name: string }) => c.name === "synthetic.float_payload_verifies");
  const result = verifyBundle(floatCase.bundle, keys);
  assert.equal(result.status, "verified");
  const guard = (
    result.claims?.constraints_evaluated as {
      jev_risk_guard: { big: unknown; latency_ms: unknown; injected_instructions: unknown; tiny: unknown };
    }
  ).jev_risk_guard;
  assert.equal(guard.big, "9007199254740993");
  assert.equal(guard.latency_ms, 0);
  assert.equal(guard.injected_instructions, 1);
  assert.equal(guard.tiny, 4e-05);
});

test("canonical JSON sorts keys and escapes non-ASCII", () => {
  assert.equal(canonicalJson({ b: 1, a: ["é", null] }), '{"a":["\\u00e9",null],"b":1}');
});

test("a caller-supplied key of the wrong length is malformed, not invalid", () => {
  const valid = cases.find((c: any) => c.expect === "verified");
  const signedKid = JSON.parse(valid.bundle.signing_input).kid;
  const shortKey = new Map([[signedKid, Buffer.alloc(16)]]);

  // A broken caller key must read as "this verifier cannot judge", matching
  // the Python verifier. Reporting it as invalid would allege forgery for a
  // local configuration error.
  assert.equal(verifyBundle(valid.bundle, shortKey).status, "malformed");
});
