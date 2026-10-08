import { test } from "node:test";
import assert from "node:assert/strict";
import { createHash, generateKeyPairSync, sign, type KeyObject } from "node:crypto";
import { canonicalJson, keySetFromDocument, verifyBundle } from "../src/verify.ts";

function rawPublic(key: KeyObject): Buffer {
  const x = key.export({ format: "jwk" }).x;
  if (typeof x !== "string") throw new Error("test key is missing its public x");
  return Buffer.from(x, "base64url");
}

const { publicKey, privateKey } = generateKeyPairSync("ed25519");
const raw = rawPublic(publicKey);
const keys = new Map([["k", raw]]);

function signatureOver(bytes: Buffer): string {
  return sign(null, bytes, privateKey).toString("base64");
}

function signed(signingInput: string, envelope: Record<string, unknown> = {}) {
  return {
    receipt_id: "r",
    kid: "k",
    alg: "Ed25519",
    canonicalization: "awi-canonical-json/1",
    signing_input: signingInput,
    signature: signatureOver(Buffer.from(signingInput, "utf8")),
    ...envelope,
  };
}

test("a lone surrogate must not verify as the character U+FFFD", () => {
  const honest = '{"kid":"k","note":"\uFFFD","receipt_id":"r"}';
  const swapped = '{"kid":"k","note":"\uD800","receipt_id":"r"}';
  assert.equal(Buffer.from(honest, "utf8").equals(Buffer.from(swapped, "utf8")), true);
  assert.equal(JSON.parse(honest).note === JSON.parse(swapped).note, false);

  const result = verifyBundle(
    {
      receipt_id: "r",
      kid: "k",
      signing_input: swapped,
      signature: signatureOver(Buffer.from(honest, "utf8")),
    },
    keys,
  );

  assert.equal(result.status, "malformed");
  assert.equal(result.ok, false);
  assert.equal(result.claims, undefined);
});

test("an escaped lone surrogate in the signed payload is malformed", () => {
  const signingInput = '{"kid":"k","note":"\\ud800","receipt_id":"r"}';
  assert.equal(JSON.parse(signingInput).note.isWellFormed(), false);

  const result = verifyBundle(signed(signingInput), keys);

  assert.equal(result.status, "malformed");
  assert.equal(result.ok, false);
});

test("a lone surrogate in a signed key or nested array is malformed", () => {
  const asKey = '{"\\ud800":"x","kid":"k","receipt_id":"r"}';
  const nested = '{"kid":"k","notes":["ok","\\udfff"],"receipt_id":"r"}';

  assert.equal(verifyBundle(signed(asKey), keys).status, "malformed");
  assert.equal(verifyBundle(signed(nested), keys).status, "malformed");
});

test("a signed emoji still verifies", () => {
  const note = "\uD83D\uDE00";
  const body = { kid: "k", note, receipt_id: "r" };
  const payloadHash = createHash("sha256").update(canonicalJson(body), "utf8").digest("hex");
  const signingInput = canonicalJson({ ...body, payload_hash: payloadHash });
  const result = verifyBundle(signed(signingInput), keys);

  assert.equal(result.status, "verified");
  assert.equal(result.ok, true);
  assert.equal(result.claims?.note, note);
  assert.equal(result.receiptId, "r");
  assert.equal(result.keyId, "k");
});

test("the replacement character itself still verifies", () => {
  const signingInput = '{"kid":"k","note":"\uFFFD","receipt_id":"r"}';
  const result = verifyBundle(signed(signingInput), keys);

  assert.equal(result.status, "verified");
  assert.equal(result.claims?.note, "\uFFFD");
});

test("a public key that is not 32 bytes is malformed, not a bad signature", () => {
  const bundle = signed('{"kid":"k","receipt_id":"r"}');

  for (const key of [Buffer.from("too-short"), Buffer.alloc(31), Buffer.alloc(33), "not-bytes"]) {
    const result = verifyBundle(bundle, new Map([["k", key]]));
    assert.equal(result.status, "malformed", `status for ${key}`);
    assert.equal(result.ok, false);
    assert.match(String(result.reason), /32-byte/);
  }
});

test("a Uint8Array public key of 32 bytes verifies", () => {
  const result = verifyBundle(signed('{"kid":"k","receipt_id":"r"}'), new Map([["k", new Uint8Array(raw)]]));

  assert.equal(result.status, "verified");
});

test("a different 32-byte key is an invalid signature", () => {
  const result = verifyBundle(signed('{"kid":"k","receipt_id":"r"}'), new Map([["k", Buffer.alloc(32, 7)]]));

  assert.equal(result.status, "invalid");
  assert.equal(result.ok, false);
});

test("a short envelope key with an unknown signed kid stays unknown", () => {
  const signingInput = '{"kid":"missing","receipt_id":"r"}';
  const bundle = {
    kid: "k",
    signing_input: signingInput,
    signature: signatureOver(Buffer.from(signingInput, "utf8")),
    receipt_id: "r",
  };

  assert.equal(verifyBundle(bundle, new Map([["k", Buffer.from("short")]])).status, "unknown_key");
});

test("a very deep signed payload is malformed instead of throwing", () => {
  const open = '{"a":'.repeat(8000);
  const close = "}".repeat(8000);
  const signingInput = `{"deep":${open}0${close},"kid":"k","payload_hash":"abc","receipt_id":"r"}`;

  const result = verifyBundle(signed(signingInput), keys);

  assert.equal(result.status, "malformed");
  assert.equal(result.ok, false);
});

test("a JSON string bundle verifies the same as an object", () => {
  const bundle = signed('{"kid":"k","receipt_id":"r"}');

  assert.equal(verifyBundle(JSON.stringify(bundle), keys).status, "verified");
  assert.equal(verifyBundle("{oops", keys).status, "malformed");
  assert.equal(verifyBundle("[]", keys).status, "malformed");
});

test("UTF-8 bytes of a bundle verify, and invalid UTF-8 is malformed", () => {
  const bundle = signed('{"kid":"k","receipt_id":"r"}');
  const bytes = Buffer.from(JSON.stringify(bundle), "utf8");

  assert.equal(verifyBundle(bytes, keys).status, "verified");
  assert.equal(verifyBundle(new Uint8Array(bytes), keys).status, "verified");
  assert.equal(verifyBundle(Buffer.from([0xff, 0xfe, 0x00]), keys).status, "malformed");
});

test("a disabled key is dropped and does not count as a bad signature", () => {
  const document = {
    keys: [
      { kid: "k", alg: "Ed25519", status: "disabled", public_key_b64: raw.toString("base64") },
      { kid: "live", alg: "Ed25519", status: "active", public_key_b64: Buffer.alloc(32, 2).toString("base64") },
    ],
  };
  const loaded = keySetFromDocument(document);

  assert.equal(loaded.has("k"), false);
  assert.equal(loaded.has("live"), true);
  assert.equal(verifyBundle(signed('{"kid":"k","receipt_id":"r"}'), loaded).status, "unknown_key");
});

test("a JWK-only key and a bad raw key with a good JWK both load", () => {
  const x = raw.toString("base64url");
  const loaded = keySetFromDocument({
    keys: [
      { kid: "jwk", alg: "Ed25519", jwk: { crv: "Ed25519", x } },
      { kid: "fallback", alg: "Ed25519", public_key_b64: "AAAA", jwk: { crv: "Ed25519", x } },
      { kid: "skip", alg: "RS256", public_key_b64: raw.toString("base64") },
      { kid: "", public_key_b64: raw.toString("base64") },
      "nope",
    ],
  });

  assert.deepEqual([...loaded.keys()].sort(), ["fallback", "jwk"]);
  assert.equal(verifyBundle(signed('{"kid":"k","receipt_id":"r"}'), new Map([["k", loaded.get("jwk")]])).status, "verified");
});

test("issuer mismatch is rejected before the signature could matter", () => {
  const bundle = signed('{"kid":"k","receipt_id":"r"}', { issuer: "https://api.example.com" });

  assert.equal(verifyBundle(bundle, keys, "https://other.example.com").status, "mismatch");
  assert.equal(verifyBundle(bundle, keys, "https://api.example.com").status, "verified");
});

test("envelope algorithm and canonicalization disagreements are mismatches", () => {
  const bundle = signed('{"kid":"k","receipt_id":"r"}');

  assert.equal(verifyBundle({ ...bundle, alg: "RS256" }, keys).status, "mismatch");
  assert.equal(verifyBundle({ ...bundle, canonicalization: "other/9" }, keys).status, "mismatch");
  assert.equal(verifyBundle({ ...bundle, receipt_id: "other" }, keys).status, "mismatch");
});

test("a signed algorithm this verifier does not implement is unsupported", () => {
  const signingInput = '{"alg":"RS256","kid":"k","receipt_id":"r"}';

  assert.equal(verifyBundle(signed(signingInput), keys).status, "unsupported");
});

test("a payload hash that does not match is a mismatch", () => {
  const signingInput = '{"kid":"k","payload_hash":"0000","receipt_id":"r"}';

  assert.equal(verifyBundle(signed(signingInput), keys).status, "mismatch");
});

test("a kid on the envelope that is not the signed kid is a mismatch", () => {
  const other = generateKeyPairSync("ed25519");
  const otherRaw = rawPublic(other.publicKey);
  const signingInput = '{"kid":"other","receipt_id":"r"}';
  const bundle = {
    kid: "k",
    signing_input: signingInput,
    signature: signatureOver(Buffer.from(signingInput, "utf8")),
    receipt_id: "r",
  };

  assert.equal(verifyBundle(bundle, new Map([["k", raw]])).status, "mismatch");
  assert.equal(
    verifyBundle(bundle, new Map([["k", raw], ["other", otherRaw]])).status,
    "invalid",
  );
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
