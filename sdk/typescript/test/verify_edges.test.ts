import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { canonicalJson, keySetFromDocument, verifyBundle } from "../src/verify.ts";

const vectors = join(dirname(fileURLToPath(import.meta.url)), "..", "..", "..", "spec", "test-vectors");
const cases = JSON.parse(readFileSync(join(vectors, "receipts.json"), "utf8")).cases as {
  name: string;
  bundle: { signing_input: string; signature: string; kid: string; receipt_id: string; alg: string; canonicalization: string };
}[];
const recorded = cases.find((c) => c.name === "success_legacy.valid")!.bundle;
const keys = keySetFromDocument(JSON.parse(readFileSync(join(vectors, "keys.json"), "utf8")));
const claims = JSON.parse(recorded.signing_input) as { kid: string; receipt_id: string; tool: string; alg: string; payload_hash: string };
const raw = keys.get(claims.kid);
assert.ok(Buffer.isBuffer(raw));

function swapField(field: keyof typeof claims, value: string): typeof recorded {
  const original = '"' + field + '":' + JSON.stringify(claims[field]);
  assert.ok(recorded.signing_input.includes(original));
  return { ...recorded, signing_input: recorded.signing_input.replace(original, '"' + field + '":' + value) };
}

test("the recorded gateway receipt verifies", () => {
  const result = verifyBundle(recorded, keys);
  assert.equal(result.status, "verified");
  assert.equal(result.claims?.tool, claims.tool);
  assert.equal(result.receiptId, claims.receipt_id);
  assert.equal(result.keyId, claims.kid);
});

test("a lone surrogate must not verify as the character U+FFFD", () => {
  const honest = swapField("tool", '"\uFFFD"');
  const swapped = swapField("tool", '"\uD800"');
  assert.equal(Buffer.from(honest.signing_input, "utf8").equals(Buffer.from(swapped.signing_input, "utf8")), true);
  assert.notEqual(JSON.parse(honest.signing_input).tool, JSON.parse(swapped.signing_input).tool);
  const result = verifyBundle(swapped, keys);
  assert.equal(result.status, "malformed");
  assert.equal(result.ok, false);
  assert.equal(result.claims, undefined);
});

test("an escaped lone surrogate in the recorded payload is malformed", () => {
  const bundle = swapField("tool", '"\\ud800"');
  assert.equal(JSON.parse(bundle.signing_input).tool.isWellFormed(), false);
  assert.equal(verifyBundle(bundle, keys).status, "malformed");
});

test("a lone surrogate in a swapped key or nested array is malformed", () => {
  const asKey = { ...recorded, signing_input: recorded.signing_input.replace(/}$/, ',"\\ud800":"x"}') };
  const nested = { ...recorded, signing_input: recorded.signing_input.replace(/}$/, ',"notes":["ok","\\udfff"]}') };
  assert.equal(verifyBundle(asKey, keys).status, "malformed");
  assert.equal(verifyBundle(nested, keys).status, "malformed");
});

test("well-formed Unicode mutations reach signature verification", () => {
  for (const value of ['"\\ud83d\\ude00"', '"\uFFFD"']) {
    assert.equal(verifyBundle(swapField("tool", value), keys).status, "invalid");
  }
});

test("a public key that is not 32 bytes is malformed, not a bad signature", () => {
  for (const key of [raw.subarray(0, 31), Buffer.concat([raw, raw.subarray(0, 1)]), "not-bytes"]) {
    const result = verifyBundle(recorded, new Map([[claims.kid, key]]));
    assert.equal(result.status, "malformed");
    assert.equal(result.ok, false);
    assert.match(String(result.reason), /32-byte/);
  }
});

test("a Uint8Array public key of 32 bytes verifies", () => {
  assert.equal(verifyBundle(recorded, new Map([[claims.kid, new Uint8Array(raw)]])).status, "verified");
});

test("a mutated 32-byte public key gives an invalid signature", () => {
  const wrong = Buffer.from(raw);
  wrong[0] ^= 1;
  assert.equal(verifyBundle(recorded, new Map([[claims.kid, wrong]])).status, "invalid");
});

test("an unknown signed kid stays unknown with a short envelope key", () => {
  const bundle = swapField("kid", '"missing"');
  assert.equal(verifyBundle(bundle, new Map([[claims.kid, raw.subarray(0, 31)]])).status, "unknown_key");
});

test("a very deep swapped payload is handled without throwing", () => {
  const deep = '{"a":'.repeat(8000) + "0" + "}".repeat(8000);
  const bundle = { ...recorded, signing_input: recorded.signing_input.replace(/}$/, ',"deep":' + deep + '}') };
  assert.equal(verifyBundle(bundle, keys).status, "invalid");
});

test("JSON string and UTF-8 bytes of a recorded bundle verify", () => {
  const serialized = JSON.stringify(recorded);
  assert.equal(verifyBundle(serialized, keys).status, "verified");
  assert.equal(verifyBundle(Buffer.from(serialized, "utf8"), keys).status, "verified");
  assert.equal(verifyBundle(new Uint8Array(Buffer.from(serialized, "utf8")), keys).status, "verified");
  assert.equal(verifyBundle("{oops", keys).status, "malformed");
  assert.equal(verifyBundle("[]", keys).status, "malformed");
  assert.equal(verifyBundle(Buffer.from([0xff, 0xfe, 0x00]), keys).status, "malformed");
});

test("a disabled recorded key is dropped", () => {
  const loaded = keySetFromDocument({ keys: [{ kid: claims.kid, alg: "Ed25519", status: "disabled", public_key_b64: raw.toString("base64") }] });
  assert.equal(loaded.has(claims.kid), false);
  assert.equal(verifyBundle(recorded, loaded).status, "unknown_key");
});

test("a JWK-only key and a short raw key with a good JWK both load", () => {
  const jwk = { crv: "Ed25519", x: raw.toString("base64url") };
  const jwkOnly = keySetFromDocument({ keys: [{ kid: claims.kid, alg: "Ed25519", jwk }] });
  const fallback = keySetFromDocument({ keys: [
    { kid: claims.kid, alg: "Ed25519", public_key_b64: raw.subarray(0, 1).toString("base64"), jwk },
  ] });
  assert.equal(verifyBundle(recorded, jwkOnly).status, "verified");
  assert.equal(verifyBundle(recorded, fallback).status, "verified");
});

test("envelope disagreements are mismatches", () => {
  assert.equal(verifyBundle({ ...recorded, alg: "RS256" }, keys).status, "mismatch");
  assert.equal(verifyBundle({ ...recorded, canonicalization: "other/9" }, keys).status, "mismatch");
  assert.equal(verifyBundle({ ...recorded, receipt_id: recorded.receipt_id + "-other" }, keys).status, "mismatch");
  assert.equal(verifyBundle({ ...recorded, kid: "other" }, keys).status, "mismatch");
});

test("swapping signed algorithm, hash, or kid invalidates the recorded signature", () => {
  assert.equal(verifyBundle(swapField("alg", '"RS256"'), keys).status, "invalid");
  const hash = claims.payload_hash;
  const changedHash = (hash[0] === "0" ? "1" : "0") + hash.slice(1);
  assert.equal(verifyBundle(swapField("payload_hash", JSON.stringify(changedHash)), keys).status, "invalid");
  assert.equal(verifyBundle(swapField("kid", '"other"'), new Map([["other", raw]])).status, "invalid");
});

test("canonical JSON matches the shared escaping rules", () => {
  assert.equal(canonicalJson({ k: "\u007f" }), '{"k":"\\u007f"}');
  assert.equal(canonicalJson({ k: "\u0080" }), '{"k":"\\u0080"}');
  assert.equal(canonicalJson({ k: "a\nb" }), '{"k":"a\\nb"}');
  assert.equal(canonicalJson({ k: "\uD83D\uDE00" }), '{"k":"\\ud83d\\ude00"}');
});

test("a key set document without a keys list throws", () => {
  assert.throws(() => keySetFromDocument({ nope: [] }), /keys/);
  assert.throws(() => keySetFromDocument(null), /keys/);
});
