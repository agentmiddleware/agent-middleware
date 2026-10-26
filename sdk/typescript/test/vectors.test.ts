import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { canonicalJson, keySetFromDocument, verifyBundle, verifyPermitBundle } from "../src/verify.ts";

const vectors = join(dirname(fileURLToPath(import.meta.url)), "..", "..", "..", "spec", "test-vectors");
const keys = keySetFromDocument(JSON.parse(readFileSync(join(vectors, "keys.json"), "utf8")));
const receiptCases = JSON.parse(readFileSync(join(vectors, "receipts.json"), "utf8")).cases;
const permitCases = JSON.parse(readFileSync(join(vectors, "permits.json"), "utf8")).cases;

const suites = [
  { file: "receipts.json", verify: verifyBundle },
  { file: "rotation.json", verify: verifyBundle },
  { file: "permits.json", verify: verifyPermitBundle },
] as const;

for (const { file, verify } of suites) {
  const cases = file === "receipts.json" ? receiptCases : file === "permits.json" ? permitCases : JSON.parse(readFileSync(join(vectors, file), "utf8")).cases;
  for (const c of cases) {
    test(`${file}:${c.name}`, () => {
      const ks = c.keys ? keySetFromDocument(c.keys) : keys;
      assert.equal(verify(c.bundle, ks).status, c.expect);
    });
  }
}

test("a signed receipt is not permit evidence", () => {
  const receipt = permitCases.find((c: { name: string }) => c.name === "denial_constraints.receipt_is_not_permit");
  assert.equal(verifyBundle(receipt.bundle, keys).status, "verified");
  assert.equal(verifyPermitBundle(receipt.bundle, keys).status, "malformed");
});

test("permit id mismatch reports the signed permit id in permitId", () => {
  const mismatch = permitCases.find((c: { name: string }) => c.name === "active_minimal.envelope_permit_id_mismatch");
  const signedId = JSON.parse(mismatch.bundle.signing_input).permit_id;
  const result = verifyPermitBundle(mismatch.bundle, keys);
  assert.equal(result.status, "mismatch");
  assert.equal(result.permitId, signedId);
  assert.equal(result.receiptId, undefined);
});

test("float-bearing vector keeps the signed integer above 2^53", () => {
  const floatCase = receiptCases.find((c: { name: string }) => c.name === "synthetic.float_payload_verifies");
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
  const valid = receiptCases.find((c: { expect: string }) => c.expect === "verified");
  const signedKid = JSON.parse(valid.bundle.signing_input).kid;
  const shortKey = new Map([[signedKid, Buffer.alloc(16)]]);

  // A broken caller key must read as "this verifier cannot judge", matching
  // the Python verifier. Reporting it as invalid would allege forgery for a
  // local configuration error.
  assert.equal(verifyBundle(valid.bundle, shortKey).status, "malformed");
});
