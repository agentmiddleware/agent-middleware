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

/** True when decoded JSON contains a lone Unicode surrogate. */
function hasLoneSurrogate(value: unknown): boolean {
  const pending: unknown[] = [value];
  const seen = new Set<object>();
  while (pending.length > 0) {
    const current = pending.pop();
    if (typeof current === "string") {
      for (let i = 0; i < current.length; i++) {
        const code = current.charCodeAt(i);
        if (code >= 0xd800 && code <= 0xdbff) {
          const next = current.charCodeAt(i + 1);
          if (!(next >= 0xdc00 && next <= 0xdfff)) return true;
          i += 1;
        } else if (code >= 0xdc00 && code <= 0xdfff) {
          return true;
        }
      }
    } else if (Array.isArray(current)) {
      if (seen.has(current)) continue;
      seen.add(current);
      for (const item of current) pending.push(item);
    } else if (current !== null && typeof current === "object") {
      if (seen.has(current)) continue;
      seen.add(current);
      const obj = current as Record<string, unknown>;
      for (const key of Object.keys(obj)) {
        pending.push(key);
        pending.push(obj[key]);
      }
    }
  }
  return false;
}

function edOk(raw: Buffer, sig: Buffer, msg: Buffer): boolean {
  try {
    const key = createPublicKey({ key: { kty: "OKP", crv: "Ed25519", x: raw.toString("base64url") }, format: "jwk" });
    return edVerify(null, msg, key, sig);
  } catch {
    return false;
  }
}

/** Verify one portable receipt bundle against a caller-supplied key set. */
export function verifyBundle(bundle: unknown, keySet: KeySet, expectedIssuer?: string): VerificationResult {
  if (typeof bundle === "string") {
    try {
      bundle = JSON.parse(bundle);
    } catch {
      return fail("malformed", "bundle is not valid JSON");
    }
  }
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
  // Same order as the Python verifier: unreadable text is malformed, and
  // that decision is made before any signature check. A bad signature must
  // not turn "cannot read the text" into "tampered".
  if (hasLoneSurrogate(payload)) {
    return fail("malformed", "signing_input contains invalid Unicode text", envKid);
  }
  const p = payload as Record<string, unknown>;
  const kid = p.kid;
  if (typeof kid !== "string" || !kid) return fail("malformed", "signed payload has no kid", envKid);
  // Judge alg and canonicalization only after the signature check. Until
  // then they are bytes an editor can change, and treating them as a
  // capability gap ("unsupported") hides tampering.
  const alg = p.alg === undefined ? "Ed25519" : p.alg;
  const signedCanon = p.canonicalization;

  const sig = b64(sigB64);
  if (!sig) return fail("malformed", "signature is not valid base64", kid);
  if (sig.length !== SIG_LEN) return fail("malformed", "signature is not a 64-byte Ed25519 signature", kid);
  const msg = Buffer.from(signingInput, "utf8");

  const raw = keySet.get(kid);
  // A key that is present but the wrong length cannot be judged. Reporting
  // that as invalid would call the receipt tampered.
  if (raw && raw.length !== RAW_KEY_LEN) {
    return fail("malformed", `public key for kid ${kid} is not a 32-byte Ed25519 key`, kid);
  }
  if (!raw) {
    const envKey = envKid !== kid ? keySet.get(envKid) : undefined;
    if (envKey && envKey.length === RAW_KEY_LEN && edOk(envKey, sig, msg)) {
      return fail("mismatch", `signed under kid ${envKid} but the payload names kid ${kid}`, kid);
    }
    return fail("unknown_key", `no published key for kid ${kid}`, kid);
  }
  if (raw.length !== RAW_KEY_LEN) return fail("malformed", `public key for kid ${kid} is not a 32-byte Ed25519 key`, kid);
  if (!edOk(raw, sig, msg)) return fail("invalid", "signature does not verify over signing_input", kid);

  if (alg !== "Ed25519") return fail("unsupported", `unsupported signature algorithm: ${String(alg)}`, kid);
  if (typeof signedCanon === "string" && signedCanon !== CANONICALIZATION) {
    return fail("unsupported", `unsupported canonicalization: ${signedCanon}`, kid);
  }

  if (envKid !== kid) return fail("mismatch", "bundle names a different kid than the signed payload", kid);
  const envelopeAlg = b.alg === undefined ? "Ed25519" : b.alg;
  if (envelopeAlg !== alg) return fail("mismatch", "bundle algorithm differs from the signed payload", kid);
  const expectedCanon = typeof signedCanon === "string" ? signedCanon : CANONICALIZATION;
  const envelopeCanon = b.canonicalization === undefined ? CANONICALIZATION : b.canonicalization;
  if (envelopeCanon !== expectedCanon) return fail("mismatch", "bundle canonicalization differs from the signed payload", kid);

  if (typeof p.payload_hash === "string") {
    const inner: Record<string, unknown> = { ...p };
    delete inner.payload_hash;
    let canonical: string;
    try {
      canonical = canonicalJson(inner);
    } catch {
      return fail("malformed", "signed payload exceeds the supported JSON nesting depth", kid);
    }
    const computed = createHash("sha256").update(canonical, "utf8").digest("hex");
    if (computed !== p.payload_hash) return fail("mismatch", "payload_hash does not match the signed payload", kid);
  }
  const receiptId = p.receipt_id;
  if (typeof receiptId !== "string" || !receiptId) return fail("malformed", "signed payload has no receipt_id", kid);
  if (b.receipt_id !== undefined && b.receipt_id !== null && b.receipt_id !== receiptId) return fail("mismatch", "bundle receipt_id does not match the signed receipt_id", kid, receiptId);
  return { status: "verified", ok: true, receiptId, keyId: kid, claims: p };
}
