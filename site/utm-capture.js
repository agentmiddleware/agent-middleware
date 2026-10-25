// Persists campaign attribution for this tab. demo-key.js reads the same
// sessionStorage record and repeats this capture so a direct /demo-key/ visit
// still works when this file did not run. UTM values are capped at the API's
// 128-character limit and the referrer at 512 before they are stored, so an
// overlong query string cannot stick and later 422.
const UTM_PARAMS = ["utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term"];
const UTM_STORAGE_KEY = "amwDemoKeyUtm";
const UTM_MAX_LENGTH = 128;
const REFERRER_MAX_LENGTH = 512;

function capped(value, max) {
  if (typeof value !== "string") return "";
  const trimmed = value.trim();
  if (!trimmed) return "";
  return trimmed.length > max ? trimmed.slice(0, max) : trimmed;
}

function isExternalReferrer(referrer) {
  if (!referrer) return false;
  try {
    return new URL(referrer).origin !== location.origin;
  } catch {
    return false;
  }
}

function readStoredAttribution() {
  try {
    const raw = sessionStorage.getItem(UTM_STORAGE_KEY);
    if (!raw) return { utm: {}, referrer: "" };
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object") return { utm: {}, referrer: "" };
    const utm = {};
    for (const key of UTM_PARAMS) {
      const value = capped(parsed[key], UTM_MAX_LENGTH);
      if (value) utm[key] = value;
    }
    const referrer = capped(parsed.referrer, REFERRER_MAX_LENGTH);
    return { utm, referrer: isExternalReferrer(referrer) ? referrer : "" };
  } catch {
    return { utm: {}, referrer: "" };
  }
}

function utmFromLocation() {
  const params = new URLSearchParams(location.search || "");
  const found = {};
  for (const key of UTM_PARAMS) {
    const value = capped(params.get(key), UTM_MAX_LENGTH);
    if (value) found[key] = value;
  }
  return found;
}

function pageReferrer() {
  return capped(typeof document.referrer === "string" ? document.referrer : "", REFERRER_MAX_LENGTH);
}

function captureAttribution() {
  const stored = readStoredAttribution();
  // URL values override the same key already stored; absent URL params do not clear it.
  const utm = { ...stored.utm, ...utmFromLocation() };
  const current = pageReferrer();
  const referrer = stored.referrer || (isExternalReferrer(current) ? current : "");
  const record = { ...utm };
  if (referrer) record.referrer = referrer;
  return record;
}

function persistAttribution(record) {
  if (Object.keys(record).length === 0) return;
  try {
    sessionStorage.setItem(UTM_STORAGE_KEY, JSON.stringify(record));
  } catch {
    // Storage can be blocked; a later demo-key visit still sends its own URL values.
  }
}

persistAttribution(captureAttribution());
