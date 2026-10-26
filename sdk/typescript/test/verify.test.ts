import { test } from "node:test";
import assert from "node:assert/strict";
import { createHash, generateKeyPairSync, sign } from "node:crypto";
import {
  CANONICALIZATION,
  P256_ALGORITHM,
  canonicalJson,
  keySetFromDocument,
  verifyBundle,
  type KeySet,
  type VerificationResult,
} from "../src/verify.ts";

const keyPair = generateKeyPairSync("ed25519");
const otherPair = generateKeyPairSync("ed25519");
const p256Pair = generateKeyPairSync("ec", { namedCurve: "prime256v1" });
const otherP256Pair = generateKeyPairSync("ec", { namedCurve: "prime256v1" });
const p256PublicDer = p256Pair.publicKey.export({ type: "spki", format: "der" });
const rawKey = Buffer.from(
  (keyPair.publicKey.export({ format: "jwk" }).x as string),
  "base64url",
);
const otherRawKey = Buffer.from(
  (otherPair.publicKey.export({ format: "jwk" }).x as string),
  "base64url",
);

function keys(): KeySet {
  return new Map([["key-1", rawKey]]);
}

function payload(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return { receipt_id: "rcpt-1", kid: "key-1", ...overrides };
}

/** Build an envelope around payload, signed with the test key. */
function seal(
  body: Record<string, unknown>,
  kid = "key-1",
  envelope: Record<string, unknown> = {},
): Record<string, unknown> {
  const signingInput = JSON.stringify(body);
  const signature = sign(
    null,
    Buffer.from(signingInput, "utf8"),
    keyPair.privateKey,
  ).toString("base64");
  return { signing_input: signingInput, signature, kid, ...envelope };
}

function statusOf(bundle: unknown, ks: KeySet = keys(), issuer?: string): string {
  return verifyBundle(bundle, ks, issuer).status;
}

function p256Keys(status = "active", publicKeyDer = p256PublicDer): KeySet {
  return keySetFromDocument({
    keys: [{ kid: "p256-key", alg: P256_ALGORITHM, status, public_key_b64: publicKeyDer.toString("base64") }],
  });
}

function sealP256(body: Record<string, unknown> = { receipt_id: "rcpt-p256", kid: "p256-key", alg: P256_ALGORITHM }): Record<string, unknown> {
  const signingInput = JSON.stringify(body);
  const signature = sign("sha256", Buffer.from(signingInput, "utf8"), p256Pair.privateKey).toString("base64");
  return { signing_input: signingInput, signature, kid: "p256-key", alg: P256_ALGORITHM };
}

test("valid bundle verifies and reports claims from the signed bytes", () => {
  const result = verifyBundle(
    seal(payload({ tool: "partner.echo", credits_charged: "1" })),
    keys(),
  );
  assert.equal(result.status, "verified");
  assert.equal(result.ok, true);
  assert.equal(result.receiptId, "rcpt-1");
  assert.equal(result.keyId, "key-1");
  assert.equal(result.claims?.tool, "partner.echo");
  assert.equal(result.claims?.credits_charged, "1");
});

test("verification is stateless: the same bundle verifies twice", () => {
  const bundle = seal(payload());
  assert.equal(verifyBundle(bundle, keys()).status, "verified");
  assert.equal(verifyBundle(bundle, keys()).status, "verified");
});

test("one altered byte in the signed payload is invalid, not a mismatch", () => {
  const bundle = seal(payload({ credits_charged: "100", credits_authorized: "100" }));
  const tampered = {
    ...bundle,
    signing_input: (bundle.signing_input as string).replace('"100"', '"900"'),
  };
  const result = verifyBundle(tampered, keys());
  assert.equal(result.status, "invalid");
  assert.equal(result.ok, false);
});

test("signature from a different key is invalid", () => {
  const signingInput = JSON.stringify(payload());
  const signature = sign(
    null,
    Buffer.from(signingInput, "utf8"),
    otherPair.privateKey,
  ).toString("base64");
  const result = verifyBundle(
    { signing_input: signingInput, signature, kid: "key-1" },
    keys(),
  );
  assert.equal(result.status, "invalid");
});

test("known kid mapped to the wrong key bytes is invalid", () => {
  const result = verifyBundle(seal(payload()), new Map([["key-1", otherRawKey]]));
  assert.equal(result.status, "invalid");
});

test("short public key bytes are malformed, not evidence of tampering", () => {
  const result = verifyBundle(
    seal(payload()),
    new Map([["key-1", Buffer.from("too-short")]]),
  );
  assert.equal(result.status, "malformed");
  assert.equal(result.ok, false);
});

test("unknown kid is unknown_key even when other keys are held", () => {
  const signingInput = JSON.stringify(payload({ kid: "ghost" }));
  const signature = sign(
    null,
    Buffer.from(signingInput, "utf8"),
    otherPair.privateKey,
  ).toString("base64");
  const result = verifyBundle(
    { signing_input: signingInput, signature, kid: "key-1" },
    keys(),
  );
  assert.equal(result.status, "unknown_key");
  assert.equal(result.keyId, "ghost");
});

test("empty key set reports unknown_key, not invalid", () => {
  assert.equal(statusOf(seal(payload()), new Map()), "unknown_key");
});

test("kid confusion that verifies under the envelope key is mismatch", () => {
  const signingInput = JSON.stringify(payload({ kid: "ghost" }));
  const signature = sign(
    null,
    Buffer.from(signingInput, "utf8"),
    keyPair.privateKey,
  ).toString("base64");
  const bundle = { signing_input: signingInput, signature, kid: "key-1" };
  const result = verifyBundle(bundle, keys());
  assert.equal(result.status, "mismatch");
  assert.match(result.reason ?? "", /ghost/);
});

test("kid confusion with an unusable envelope key stays unknown_key", () => {
  const signingInput = JSON.stringify(payload({ kid: "ghost" }));
  const signature = sign(
    null,
    Buffer.from(signingInput, "utf8"),
    keyPair.privateKey,
  ).toString("base64");
  const bundle = { signing_input: signingInput, signature, kid: "key-1" };
  const result = verifyBundle(bundle, new Map([["key-1", Buffer.from("short")]]));
  assert.equal(result.status, "unknown_key");
});

test("envelope kid differing from a verified payload is mismatch", () => {
  const bundle = seal(payload(), "key-2");
  assert.equal(statusOf(bundle, new Map([["key-1", rawKey]])), "mismatch");
});

test("payload_hash is advisory: a signed wrong hash still verifies", () => {
  const inner = payload({ credits_charged: "1" });
  const good = createHash("sha256").update(canonicalJson(inner), "utf8").digest("hex");
  assert.equal(statusOf(seal({ ...inner, payload_hash: good })), "verified");
  assert.equal(
    statusOf(seal({ ...inner, payload_hash: "0".repeat(64) })),
    "verified",
  );
});

test("editing payload_hash without re-signing is invalid", () => {
  const inner = payload({ credits_charged: "1" });
  const good = createHash("sha256").update(canonicalJson(inner), "utf8").digest("hex");
  const bundle = seal({ ...inner, payload_hash: good });
  const forged = {
    ...bundle,
    signing_input: (bundle.signing_input as string).replace(good, "0".repeat(64)),
  };
  assert.equal(statusOf(forged), "invalid");
});

function deepField(depth: number): unknown {
  let deep: unknown = 0;
  for (let i = 0; i < depth; i++) deep = { nest: deep };
  return deep;
}

test("deeply nested payload with payload_hash still verifies, never throws", () => {
  const body = { ...payload({ payload_hash: "0".repeat(64) }), deep: deepField(3000) };
  const result = verifyBundle(seal(body), keys());
  assert.equal(result.status, "verified");
});

test("deeply nested payload without payload_hash still verifies", () => {
  const body = { ...payload(), deep: deepField(3000) };
  assert.equal(statusOf(seal(body)), "verified");
});

test("lone surrogate in a signed value is malformed", () => {
  const signingInput = '{"kid":"key-1","note":"\\ud800","receipt_id":"rcpt-1"}';
  const signature = sign(
    null,
    Buffer.from(signingInput, "utf8"),
    keyPair.privateKey,
  ).toString("base64");
  const result = verifyBundle(
    { signing_input: signingInput, signature, kid: "key-1" },
    keys(),
  );
  assert.equal(result.status, "malformed");
});

test("lone surrogate in a signed key is malformed", () => {
  const signingInput = '{"kid":"key-1","receipt_id":"rcpt-1","\\ud800":"x"}';
  const signature = sign(
    null,
    Buffer.from(signingInput, "utf8"),
    keyPair.privateKey,
  ).toString("base64");
  const result = verifyBundle(
    { signing_input: signingInput, signature, kid: "key-1" },
    keys(),
  );
  assert.equal(result.status, "malformed");
});

test("paired surrogates (emoji) verify normally", () => {
  assert.equal(statusOf(seal(payload({ note: "😀" }))), "verified");
});

test("bundle given as a JSON string verifies like the parsed object", () => {
  const bundle = seal(payload());
  const fromString = verifyBundle(JSON.stringify(bundle), keys());
  assert.equal(fromString.status, "verified");
  assert.equal(fromString.receiptId, "rcpt-1");
});

test("bundle given as a non-JSON string is malformed", () => {
  assert.equal(statusOf("{oops"), "malformed");
});

test("null envelope receipt_id reads as absent", () => {
  const bundle = { ...seal(payload()), receipt_id: null };
  assert.equal(statusOf(bundle), "verified");
});

test("null envelope alg disagrees with the signed payload", () => {
  const bundle = { ...seal(payload()), alg: null };
  assert.equal(statusOf(bundle), "mismatch");
});

test("null envelope canonicalization disagrees with the signed payload", () => {
  const bundle = { ...seal(payload()), canonicalization: null };
  assert.equal(statusOf(bundle), "mismatch");
});

test("envelope receipt_id differing from the signed one is mismatch", () => {
  const bundle = { ...seal(payload()), receipt_id: "rcpt-2" };
  const result = verifyBundle(bundle, keys());
  assert.equal(result.status, "mismatch");
  assert.equal(result.receiptId, "rcpt-1");
});

test("envelope RS256 claim cannot turn a good Ed25519 receipt into unsupported", () => {
  const bundle = seal(payload(), "key-1", { alg: "RS256" });
  assert.equal(statusOf(bundle), "mismatch");
});

test("signed RS256 algorithm is unsupported, driven by the signer", () => {
  const bundle = seal({ ...payload(), alg: "RS256" });
  assert.equal(statusOf(bundle), "unsupported");
});

test("genuine P-256 receipt is unsupported only after its DER signature verifies", () => {
  const ks = p256Keys();
  const key = ks.get("p256-key");
  assert.ok(key && !Buffer.isBuffer(key));
  assert.equal(key.alg, P256_ALGORITHM);
  assert.deepEqual(key.publicKeyDer, p256PublicDer);

  const bundle = sealP256();
  assert.equal(statusOf(bundle, new Map()), "unknown_key");
  const result = verifyBundle(bundle, ks);
  assert.equal(result.status, "unsupported");
  assert.equal(result.ok, false);
});

test("altered P-256 signed bytes and signatures are invalid", () => {
  const bundle = sealP256();
  assert.equal(statusOf({ ...bundle, signing_input: (bundle.signing_input as string).replace("rcpt-p256", "rcpt-f256") }, p256Keys()), "invalid");
  const wrongSignature = sign("sha256", Buffer.from(bundle.signing_input as string), otherP256Pair.privateKey).toString("base64");
  assert.equal(statusOf({ ...bundle, signature: wrongSignature }, p256Keys()), "invalid");
});

test("base64-decoded malformed P-256 DER signatures are malformed", () => {
  const bundle = sealP256();
  const notDer = Buffer.alloc(70).toString("base64");
  const nonMinimalInteger = Buffer.from("300702020001020101", "hex").toString("base64");
  assert.equal(statusOf({ ...bundle, signature: notDer }, p256Keys()), "malformed");
  assert.equal(statusOf({ ...bundle, signature: nonMinimalInteger }, p256Keys()), "malformed");
});

test("missing and disabled P-256 keys are unknown_key", () => {
  const bundle = sealP256();
  assert.equal(statusOf(bundle, new Map()), "unknown_key");
  assert.equal(statusOf(bundle, p256Keys("disabled")), "unknown_key");
});

test("malformed or wrong-curve P-256 public keys are malformed", () => {
  const bundle = sealP256();
  assert.equal(statusOf(bundle, p256Keys("active", Buffer.from("bad DER"))), "malformed");
  const p384 = generateKeyPairSync("ec", { namedCurve: "secp384r1" });
  const p384Der = p384.publicKey.export({ type: "spki", format: "der" });
  assert.equal(statusOf(bundle, p256Keys("active", p384Der)), "malformed");
  assert.equal(statusOf({ ...bundle, signature: "!!!!" }, p256Keys()), "malformed");
});

test("P-256 signed kid confusion is a mismatch", () => {
  const bundle = sealP256({ receipt_id: "rcpt-p256", kid: "ghost", alg: P256_ALGORITHM });
  assert.equal(statusOf(bundle, p256Keys()), "mismatch");
});

test("P-256 verification key cannot mark an Ed25519-declared receipt verified", () => {
  const bundle = sealP256({ receipt_id: "rcpt-p256", kid: "p256-key", alg: "Ed25519" });
  assert.equal(statusOf(bundle, p256Keys()), "unsupported");
});

test("signed unknown canonicalization is unsupported", () => {
  const bundle = seal({ ...payload(), canonicalization: "other/9" });
  assert.equal(statusOf(bundle), "unsupported");
});

test("issuer expectation mismatch rejects an otherwise valid bundle", () => {
  const bundle = { ...seal(payload()), issuer: "https://a.example" };
  const result = verifyBundle(bundle, keys(), "https://b.example");
  assert.equal(result.status, "mismatch");
  assert.match(result.reason ?? "", /issuer/);
});

test("issuer expectation match verifies", () => {
  const bundle = { ...seal(payload()), issuer: "https://a.example" };
  assert.equal(verifyBundle(bundle, keys(), "https://a.example").status, "verified");
});

test("no issuer expectation means the envelope issuer is ignored", () => {
  const bundle = { ...seal(payload()), issuer: "https://a.example" };
  assert.equal(statusOf(bundle), "verified");
});

const malformedBundles: Array<[string, unknown]> = [
  ["null bundle", null],
  ["numeric bundle", 42],
  ["array bundle", [1, 2]],
  ["empty signing_input", { signing_input: "", signature: "eA==", kid: "key-1" }],
  ["empty signature", { signing_input: "{}", signature: "", kid: "key-1" }],
  ["empty kid", { signing_input: "{}", signature: "eA==", kid: "" }],
  ["signing_input not JSON", { signing_input: "not json", signature: "eA==", kid: "key-1" }],
  ["signing_input not an object", { signing_input: "[1,2]", signature: "eA==", kid: "key-1" }],
  ["payload without kid", { signing_input: '{"receipt_id":"r"}', signature: "eA==", kid: "key-1" }],
  ["payload without receipt_id", { signing_input: '{"kid":"key-1"}', signature: "eA==", kid: "key-1" }],
  ["non-base64 signature", seal(payload(), "key-1", { signature: "!!!!" })],
  ["short signature", seal(payload(), "key-1", { signature: "AA==" })],
  [
    "65-byte signature",
    seal(payload(), "key-1", { signature: Buffer.alloc(65).toString("base64") }),
  ],
  ["unpadded signature", seal(payload(), "key-1", { signature: "AA" })],
];

for (const [name, bundle] of malformedBundles) {
  test(`malformed: ${name}`, () => {
    const result: VerificationResult = verifyBundle(bundle, keys());
    assert.equal(result.status, "malformed");
    assert.equal(result.ok, false);
    assert.ok(result.reason);
  });
}

test("every failure carries ok false and a reason", () => {
  const failures: unknown[] = [
    null,
    seal(payload(), "key-1", { signature: "!!!!" }),
    seal(payload(), "ghost"),
    seal({ ...payload(), alg: "RS256" }),
    { ...seal(payload()), receipt_id: "rcpt-2" },
  ];
  for (const bundle of failures) {
    const result = verifyBundle(bundle, keys());
    assert.equal(result.ok, false);
    assert.ok(result.reason, `missing reason for ${result.status}`);
  }
});

test("canonical JSON sorts nested keys with no whitespace", () => {
  assert.equal(
    canonicalJson({ b: 1, a: { d: 2, c: 3 } }),
    '{"a":{"c":3,"d":2},"b":1}',
  );
});

test("canonical JSON escapes non-ASCII including astral characters", () => {
  assert.equal(canonicalJson({ k: "café" }), '{"k":"caf\\u00e9"}');
  assert.equal(canonicalJson({ k: "😀" }), '{"k":"\\ud83d\\ude00"}');
  assert.equal(
    canonicalJson({ z: 1, [CANONICALIZATION]: 2 }),
    '{"awi-canonical-json/1":2,"z":1}',
  );
});

test("key set reads raw, key_id alias and JWK mirror forms", () => {
  const rawB64 = rawKey.toString("base64");
  const jwkX = otherRawKey.toString("base64url");
  const ks = keySetFromDocument({
    keys: [
      { kid: "raw", alg: "Ed25519", public_key_b64: rawB64 },
      { key_id: "alias", alg: "Ed25519", public_key_b64: rawB64 },
      { kid: "jwk-only", alg: "Ed25519", jwk: { crv: "Ed25519", x: jwkX } },
    ],
  });
  assert.deepEqual(new Set(ks.keys()), new Set(["raw", "alias", "jwk-only"]));
  for (const value of ks.values()) {
    assert.ok(Buffer.isBuffer(value));
    assert.equal(value.length, 32);
  }
});

test("key set drops disabled keys, wrong algs and unusable entries", () => {
  const rawB64 = rawKey.toString("base64");
  const ks = keySetFromDocument({
    keys: [
      { kid: "revoked", alg: "Ed25519", status: "disabled", public_key_b64: rawB64 },
      { kid: "rsa", alg: "RS256", public_key_b64: rawB64 },
      { kid: "short", alg: "Ed25519", public_key_b64: "AAAA" },
      { kid: "bad-b64", alg: "Ed25519", public_key_b64: "!!!!" },
      { kid: "bad-jwk", alg: "Ed25519", jwk: { crv: "Ed25519", x: "a" } },
      { alg: "Ed25519", public_key_b64: rawB64 },
      "not-an-entry",
      { kid: "live", alg: "Ed25519", public_key_b64: rawB64 },
    ],
  });
  assert.deepEqual([...ks.keys()], ["live"]);
});

test("key set from a document with no keys list throws", () => {
  assert.throws(() => keySetFromDocument({ nope: [] }), /no 'keys' list/);
  assert.throws(() => keySetFromDocument(null), /no 'keys' list/);
});

test("disabled key verifies nothing: its receipts are unknown_key", () => {
  const rawB64 = rawKey.toString("base64");
  const ks = keySetFromDocument({
    keys: [{ kid: "key-1", status: "disabled", public_key_b64: rawB64 }],
  });
  assert.equal(verifyBundle(seal(payload()), ks).status, "unknown_key");
});

test("integer above 2^53 is returned as the signed digits", () => {
  const signingInput =
    '{"constraints_evaluated":{"jev_risk_guard":{"big":9007199254740993,"exact":9007199254740992,"neg":-9007199254740993,"note":"9007199254740993","pow10":10000000000000000,"safe":1}},"kid":"key-1","receipt_id":"rcpt-1"}';
  const signature = sign(
    null,
    Buffer.from(signingInput, "utf8"),
    keyPair.privateKey,
  ).toString("base64");
  const result = verifyBundle(
    { signing_input: signingInput, signature, kid: "key-1" },
    keys(),
  );
  assert.equal(result.status, "verified");
  const guard = (
    result.claims?.constraints_evaluated as {
      jev_risk_guard: {
        big: unknown;
        exact: unknown;
        neg: unknown;
        note: unknown;
        pow10: unknown;
        safe: unknown;
      };
    }
  ).jev_risk_guard;
  assert.equal(guard.big, "9007199254740993");
  assert.equal(typeof guard.big, "string");
  assert.notEqual(guard.big, 9007199254740992);
  // Powers of two (and 10^16) that fit in a double stay numbers.
  assert.equal(guard.exact, 9007199254740992);
  assert.equal(guard.pow10, 10000000000000000);
  assert.equal(guard.neg, "-9007199254740993");
  assert.equal(guard.note, "9007199254740993");
  assert.equal(guard.safe, 1);

  const tampered = signingInput.replace("9007199254740993", "9007199254740994");
  const bad = verifyBundle(
    { signing_input: tampered, signature, kid: "key-1" },
    keys(),
  );
  assert.equal(bad.status, "invalid");
  assert.equal(bad.claims, undefined);
});

test("float-bearing payload signed Python-style verifies", () => {
  // The gateway signs floats Python renders as 1.0/0.0. seal() would print
  // them as 1/0, so the signed bytes are built as a string instead.
  const innerJson =
    '{"constraints_evaluated":{"jev_risk_guard":{"injected_instructions":1.0,"latency_ms":0.0}},"kid":"key-1","receipt_id":"rcpt-1"}';
  const hash = createHash("sha256").update(innerJson, "utf8").digest("hex");
  const signingInput = innerJson.replace(
    ',"receipt_id"',
    `,"payload_hash":"${hash}","receipt_id"`,
  );
  assert.match(signingInput, /1\.0/);
  const signature = sign(
    null,
    Buffer.from(signingInput, "utf8"),
    keyPair.privateKey,
  ).toString("base64");
  const result = verifyBundle(
    { signing_input: signingInput, signature, kid: "key-1" },
    keys(),
  );
  assert.equal(result.status, "verified");
});

test(
  "unexpected crypto backend failure raises instead of reporting invalid",
  { skip: "needs an injectable verifier to simulate a crypto backend failure" },
  () => {
    assert.fail("needs an injectable verifier to simulate backend failure");
  },
);

test("signed null algorithm is unsupported, matching the Python verifier", () => {
  const bundle = seal({ ...payload(), alg: null });
  const result = verifyBundle(bundle, keys());
  assert.equal(result.status, "unsupported");
});
