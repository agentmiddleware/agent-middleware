/**
 * Offline verification of Agent Middleware receipt bundles.
 *
 * Mirrors the Python verifier in sdk/python (b2a_sdk.receipt_verifier):
 * every reported field is read from the signed bytes, and "cannot tell"
 * (unknown_key, malformed, unsupported) is kept apart from "rejected"
 * (invalid, mismatch). No network access; uses Node's built-in cryptography.
 *
 * Licensed under Apache-2.0.
 */
import { createPublicKey, verify as cryptoVerify } from "node:crypto";

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
  /**
   * Fields read from the signed bytes. An integer that does not fit in a JS
   * number is the signed decimal text (a string), so the claim matches the
   * bytes the signature covered.
   */
  claims?: Record<string, unknown>;
}

export const P256_ALGORITHM = "ECDSA-P256-SHA256";

export interface P256VerificationKey {
  alg: typeof P256_ALGORITHM;
  /** DER SubjectPublicKeyInfo, as returned by AWS KMS GetPublicKey. */
  publicKeyDer: Buffer;
}

export type KeySet = Map<string, Buffer | P256VerificationKey>;

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

/** Extract verification keys from a trust-keys.json document. */
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
    if (e.alg === P256_ALGORITHM) {
      // Retain malformed published material so verification reports malformed
      // instead of treating a known kid as an unknown key.
      keys.set(kid, { alg: P256_ALGORITHM, publicKeyDer: b64(e.public_key_b64 ?? "") ?? Buffer.alloc(0) });
      continue;
    }
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

const PLAIN_INTEGER = /^-?(?:0|[1-9]\d*)$/;
const JSON_NUMBER = /-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?/y;

/**
 * True when a JSON integer token is not the number JSON.parse would return.
 * 9007199254740993 becomes 9007199254740992; the signed digits must be kept.
 */
function integerLexemeRounds(lexeme: string): boolean {
  if (!PLAIN_INTEGER.test(lexeme)) return false;
  const digits = lexeme.startsWith("-") ? lexeme.slice(1) : lexeme;
  // A finite double stops near 1e308. Longer tokens are not that number, and
  // they do not need a BigInt the size of the token.
  if (digits.length > 400) return true;
  const asNumber = Number(lexeme);
  if (!Number.isFinite(asNumber)) return true;
  return BigInt(lexeme) !== BigInt(asNumber);
}

/**
 * Parse JSON, quoting integer tokens that a JS number would round.
 * Strings are copied through, so digits inside a string stay a string.
 */
function parseJsonPreservingUnsafeIntegers(text: string): unknown {
  let out = "";
  let i = 0;
  while (i < text.length) {
    const c = text[i]!;
    if (c === '"') {
      const start = i;
      i += 1;
      while (i < text.length) {
        if (text[i] === "\\") {
          i += 2;
          continue;
        }
        if (text[i] === '"') {
          i += 1;
          break;
        }
        i += 1;
      }
      out += text.slice(start, i);
      continue;
    }
    if (c === "-" || (c >= "0" && c <= "9")) {
      JSON_NUMBER.lastIndex = i;
      const match = JSON_NUMBER.exec(text);
      if (match && match.index === i) {
        const lexeme = match[0];
        i += lexeme.length;
        out += integerLexemeRounds(lexeme) ? JSON.stringify(lexeme) : lexeme;
        continue;
      }
    }
    out += c;
    i += 1;
  }
  return JSON.parse(out);
}

function edOk(raw: Buffer, sig: Buffer, msg: Buffer): boolean {
  try {
    const key = createPublicKey({ key: { kty: "OKP", crv: "Ed25519", x: raw.toString("base64url") }, format: "jwk" });
    return cryptoVerify(null, msg, key, sig);
  } catch {
    return false;
  }
}

function p256PublicKey(key: P256VerificationKey): ReturnType<typeof createPublicKey> | null {
  if (!Buffer.isBuffer(key.publicKeyDer)) return null;
  try {
    const publicKey = createPublicKey({ key: key.publicKeyDer, format: "der", type: "spki" });
    return publicKey.asymmetricKeyType === "ec" && publicKey.asymmetricKeyDetails?.namedCurve === "prime256v1"
      ? publicKey : null;
  } catch {
    return null;
  }
}

function derLength(bytes: Buffer, offset: number): [number, number] | null {
  if (offset >= bytes.length) return null;
  const first = bytes[offset++]!;
  if (first < 0x80) return [first, offset];
  const count = first & 0x7f;
  if (count === 0 || count > 6 || offset + count > bytes.length || bytes[offset] === 0) return null;
  let length = 0;
  for (let i = 0; i < count; i++) length = length * 256 + bytes[offset++]!;
  return length >= 0x80 ? [length, offset] : null;
}

function isDerEcdsaSignature(sig: Buffer): boolean {
  if (sig[0] !== 0x30) return false;
  const sequence = derLength(sig, 1);
  if (!sequence || sequence[0] !== sig.length - sequence[1]) return false;
  let offset = sequence[1];
  for (let i = 0; i < 2; i++) {
    if (sig[offset++] !== 0x02) return false;
    const integer = derLength(sig, offset);
    if (!integer || integer[0] === 0 || integer[0] > sig.length - integer[1]) return false;
    offset = integer[1];
    const first = sig[offset]!;
    if (first & 0x80 || (integer[0] > 1 && first === 0 && !(sig[offset + 1]! & 0x80))) return false;
    offset += integer[0];
  }
  return offset === sig.length;
}

function p256Ok(key: ReturnType<typeof createPublicKey>, sig: Buffer, msg: Buffer): boolean {
  try {
    return cryptoVerify("sha256", msg, key, sig);
  } catch {
    return false;
  }
}

function signatureValid(key: Buffer | P256VerificationKey, sig: Buffer, msg: Buffer): boolean {
  if (Buffer.isBuffer(key)) return key.length === RAW_KEY_LEN && sig.length === SIG_LEN && edOk(key, sig, msg);
  const publicKey = key.alg === P256_ALGORITHM ? p256PublicKey(key) : null;
  return publicKey !== null && isDerEcdsaSignature(sig) && p256Ok(publicKey, sig, msg);
}

/**
 * Shared core behind verifyBundle and verifyPermitBundle. Receipts and
 * permits share a signed-envelope shape; permits also require signed permit fields.
 */
function verifySignedBundle(
  bundle: unknown,
  keySet: KeySet,
  idField: "receipt_id" | "permit_id",
  expectedIssuer?: string,
): VerificationResult {
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
    // JSON.parse rounds integers above 2^53. Claims are read from this
    // object, so those tokens stay the signed decimal text.
    payload = parseJsonPreservingUnsafeIntegers(signingInput);
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
  const msg = Buffer.from(signingInput, "utf8");

  const raw = keySet.get(kid);
  if (!raw) {
    const envKey = envKid !== kid ? keySet.get(envKid) : undefined;
    if (envKey && signatureValid(envKey, sig, msg)) {
      return fail("mismatch", `signed under kid ${envKid} but the payload names kid ${kid}`, kid);
    }
    return fail("unknown_key", `no published key for kid ${kid}`, kid);
  }
  if (Buffer.isBuffer(raw)) {
    if (sig.length !== SIG_LEN) return fail("malformed", "signature is not a 64-byte Ed25519 signature", kid);
    if (raw.length !== RAW_KEY_LEN) return fail("malformed", `public key for kid ${kid} is not a 32-byte Ed25519 key`, kid);
    if (!edOk(raw, sig, msg)) return fail("invalid", "signature does not verify over signing_input", kid);
  } else {
    const publicKey = raw.alg === P256_ALGORITHM ? p256PublicKey(raw) : null;
    if (!publicKey) return fail("malformed", `public key for kid ${kid} is not a P-256 DER SPKI key`, kid);
    if (!isDerEcdsaSignature(sig)) return fail("malformed", "signature is not a DER ECDSA signature", kid);
    if (!p256Ok(publicKey, sig, msg)) return fail("invalid", "signature does not verify over signing_input", kid);
  }

  if (alg !== "Ed25519") return fail("unsupported", `unsupported signature algorithm: ${String(alg)}`, kid);
  if (!Buffer.isBuffer(raw)) return fail("unsupported", `unsupported verification key algorithm: ${raw.alg}`, kid);
  if (typeof signedCanon === "string" && signedCanon !== CANONICALIZATION) {
    return fail("unsupported", `unsupported canonicalization: ${signedCanon}`, kid);
  }

  if (envKid !== kid) return fail("mismatch", "bundle names a different kid than the signed payload", kid);
  const envelopeAlg = b.alg === undefined ? "Ed25519" : b.alg;
  if (envelopeAlg !== alg) return fail("mismatch", "bundle algorithm differs from the signed payload", kid);
  const expectedCanon = typeof signedCanon === "string" ? signedCanon : CANONICALIZATION;
  const envelopeCanon = b.canonicalization === undefined ? CANONICALIZATION : b.canonicalization;
  if (envelopeCanon !== expectedCanon) return fail("mismatch", "bundle canonicalization differs from the signed payload", kid);

  // payload_hash is advisory and never recomputed here. The signature covers
  // signing_input verbatim; re-serializing signed JSON can change number text.
  const objectId = p[idField];
  if (typeof objectId !== "string" || !objectId) return fail("malformed", `signed payload has no ${idField}`, kid);
  if (idField === "permit_id") {
    const stringFields = ["issuer_wallet_id", "subject_wallet_id", "max_credits", "issued_at", "expires_at", "nonce", "payload_hash"];
    const lists = ["scopes", "allowed_tools"];
    if (
      stringFields.some((name) => typeof p[name] !== "string") ||
      "receipt_id" in p ||
      !("subject_key_id" in p) ||
      (p.subject_key_id !== null && typeof p.subject_key_id !== "string") ||
      lists.some((name) => !Array.isArray(p[name]) || !(p[name] as unknown[]).every((item) => typeof item === "string")) ||
      p.status !== "active"
    ) {
      return fail("malformed", "signed payload is not a permit", kid);
    }
  }
  if (b[idField] !== undefined && b[idField] !== null && b[idField] !== objectId) {
    const mismatch = fail("mismatch", `bundle ${idField} does not match the signed ${idField}`, kid);
    if (idField === "permit_id") mismatch.permitId = objectId;
    else mismatch.receiptId = objectId;
    return mismatch;
  }
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
