/**
 * Offline verification of Agent Middleware receipt bundles.
 *
 * Mirrors the Python verifier in sdk/python (b2a_sdk.receipt_verifier):
 * every reported field is read from the signed bytes, and "cannot tell"
 * (unknown_key, malformed, unsupported) is kept apart from "rejected"
 * (invalid, mismatch). No network access; Node's built-in Ed25519 only.
 *
 * Licensed under Apache-2.0.
 */
import { createHash, createPublicKey, verify as edVerify } from "node:crypto";

export const CANONICALIZATION = "awi-canonical-json/1";

export type VerificationStatus =
  | "verified"
  | "invalid"
  | "malformed"
  | "unknown_key"
  | "mismatch"
  | "unsupported";

export interface VerificationResult {
  status: VerificationStatus;
  ok: boolean;
  reason?: string;
  receiptId?: string;
  permitId?: string;
  keyId?: string;
  claims?: Record<string, unknown>;
}

export type KeySet = Map<string, Buffer>;

const RAW_KEY_LEN = 32;
const SIG_LEN = 64;

function fail(status: VerificationStatus, reason: string, keyId?: string, receiptId?: string): VerificationResult {
  return { status, ok: false, reason, keyId, receiptId };
}

function b64(value: unknown, urlsafe = false): Buffer | null {
  if (typeof value !== "string") return null;
  if (urlsafe) {
    if (!/^[A-Za-z0-9_-]*$/.test(value)) return null;
    return Buffer.from(value, "base64url");
  }
  if (!/^[A-Za-z0-9+/]*={0,2}$/.test(value) || value.length % 4 !== 0) return null;
  return Buffer.from(value, "base64");
}

/** Keys sorted at every level, no whitespace, non-ASCII escaped as \uXXXX. */
export function canonicalJson(value: unknown): string {
  const ser = (v: unknown): string => {
    if (v === null || typeof v !== "object") {
      return JSON.stringify(v).replace(/[\u007f-\uffff]/g, (c) => "\\u" + c.charCodeAt(0).toString(16).padStart(4, "0"));
    }
    if (Array.isArray(v)) return "[" + v.map(ser).join(",") + "]";
    const obj = v as Record<string, unknown>;
    return "{" + Object.keys(obj).sort().map((k) => ser(k) + ":" + ser(obj[k])).join(",") + "}";
  };
  return ser(value);
}

/** Extract usable Ed25519 public keys from a trust-keys.json document. */
export function keySetFromDocument(doc: unknown): KeySet {
  const keys: KeySet = new Map();
  if (!doc || typeof doc !== "object" || !Array.isArray((doc as { keys?: unknown }).keys)) {
    throw new Error("key set document has no 'keys' list");
  }
  for (const entry of (doc as { keys: unknown[] }).keys) {
    if (!entry || typeof entry !== "object") continue;
    const e = entry as Record<string, unknown>;
    const kid = (e.kid ?? e.key_id) as unknown;
    if (typeof kid !== "string" || !kid) continue;
    if (e.status === "disabled") continue;
    if (e.alg !== undefined && e.alg !== null && e.alg !== "Ed25519") continue;
    let raw = b64(e.public_key_b64 ?? "");
    if (!raw || raw.length !== RAW_KEY_LEN) {
      const jwk = e.jwk as Record<string, unknown> | undefined;
      raw = jwk && jwk.crv === "Ed25519" ? b64(jwk.x ?? "", true) : null;
    }
    if (raw && raw.length === RAW_KEY_LEN) keys.set(kid, raw);
  }
  return keys;
}

function edOk(raw: Buffer, sig: Buffer, msg: Buffer): boolean {
  try {
    const key = createPublicKey({ key: { kty: "OKP", crv: "Ed25519", x: raw.toString("base64url") }, format: "jwk" });
    return edVerify(null, msg, key, sig);
  } catch {
    return false;
  }
}

/**
 * Shared core behind verifyBundle and verifyPermitBundle. Receipts and
 * permits are the same signed-envelope shape; only the id member differs
 * ("receipt_id" vs "permit_id"). Every other rule is identical.
 */
function verifySignedBundle(
  bundle: unknown,
  keySet: KeySet,
  idField: "receipt_id" | "permit_id",
  expectedIssuer?: string,
): VerificationResult {
  if (!bundle || typeof bundle !== "object" || Array.isArray(bundle)) return fail("malformed", "bundle must be a JSON object");
  const b = bundle as Record<string, unknown>;
  const signingInput = b.signing_input;
  const sigB64 = b.signature;
  const envKid = b.kid;
  if (typeof signingInput !== "string" || !signingInput) return fail("malformed", "bundle has no signing_input");
  if (typeof sigB64 !== "string" || !sigB64) return fail("malformed", "bundle has no signature");
  if (typeof envKid !== "string" || !envKid) return fail("malformed", "bundle has no kid");
  if (expectedIssuer !== undefined && b.issuer !== expectedIssuer) return fail("mismatch", "issuer mismatch", envKid);

  let payload: unknown;
  try {
    payload = JSON.parse(signingInput);
  } catch {
    return fail("malformed", "signing_input is not valid JSON", envKid);
  }
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) return fail("malformed", "signing_input is not a JSON object", envKid);
  const p = payload as Record<string, unknown>;
  const kid = p.kid;
  if (typeof kid !== "string" || !kid) return fail("malformed", "signed payload has no kid", envKid);
  const alg = p.alg ?? "Ed25519";
  if (alg !== "Ed25519") return fail("unsupported", `unsupported signature algorithm: ${String(alg)}`, kid);
  const signedCanon = p.canonicalization;
  if (typeof signedCanon === "string" && signedCanon !== CANONICALIZATION) return fail("unsupported", `unsupported canonicalization: ${signedCanon}`, kid);

  const sig = b64(sigB64);
  if (!sig) return fail("malformed", "signature is not valid base64", kid);
  if (sig.length !== SIG_LEN) return fail("malformed", "signature is not a 64-byte Ed25519 signature", kid);
  const msg = Buffer.from(signingInput, "utf8");

  const raw = keySet.get(kid);
  if (!raw) {
    const envKey = envKid !== kid ? keySet.get(envKid) : undefined;
    if (envKey && edOk(envKey, sig, msg)) return fail("mismatch", `signed under kid ${envKid} but the payload names kid ${kid}`, kid);
    return fail("unknown_key", `no published key for kid ${kid}`, kid);
  }
  if (!edOk(raw, sig, msg)) return fail("invalid", "signature does not verify over signing_input", kid);

  if (envKid !== kid) return fail("mismatch", "bundle names a different kid than the signed payload", kid);
  if ((b.alg ?? "Ed25519") !== alg) return fail("mismatch", "bundle algorithm differs from the signed payload", kid);
  const expectedCanon = typeof signedCanon === "string" ? signedCanon : CANONICALIZATION;
  if ((b.canonicalization ?? CANONICALIZATION) !== expectedCanon) return fail("mismatch", "bundle canonicalization differs from the signed payload", kid);

  if (typeof p.payload_hash === "string") {
    const inner: Record<string, unknown> = { ...p };
    delete inner.payload_hash;
    const computed = createHash("sha256").update(canonicalJson(inner), "utf8").digest("hex");
    if (computed !== p.payload_hash) return fail("mismatch", "payload_hash does not match the signed payload", kid);
  }
  const objectId = p[idField];
  if (typeof objectId !== "string" || !objectId) return fail("malformed", `signed payload has no ${idField}`, kid);
  if (b[idField] !== undefined && b[idField] !== objectId)
    return fail("mismatch", `bundle ${idField} does not match the signed ${idField}`, kid, objectId);
  const done: VerificationResult = { status: "verified", ok: true, keyId: kid, claims: p };
  if (idField === "permit_id") done.permitId = objectId;
  else done.receiptId = objectId;
  return done;
}

/** Verify one portable receipt bundle against a caller-supplied key set. */
export function verifyBundle(bundle: unknown, keySet: KeySet, expectedIssuer?: string): VerificationResult {
  return verifySignedBundle(bundle, keySet, "receipt_id", expectedIssuer);
}

/**
 * Verify one portable permit bundle against a caller-supplied key set.
 * Same envelope shape and status taxonomy as verifyBundle, with permit_id
 * in place of receipt_id. Signature only: expiry and revocation are enforced
 * by the gateway at validation time, so a verified permit must still be
 * rechecked live before it is honored.
 */
export function verifyPermitBundle(bundle: unknown, keySet: KeySet, expectedIssuer?: string): VerificationResult {
  return verifySignedBundle(bundle, keySet, "permit_id", expectedIssuer);
}
