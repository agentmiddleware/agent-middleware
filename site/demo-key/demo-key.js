// Classic scripts share one global lexical scope. This page loads
// /utm-capture.js first, and that file declares the same const names, so
// this copy stays inside a function. A direct visit that never loaded
// utm-capture.js still attributes from this copy.
(function () {
const issueButton = document.getElementById("get-demo-key");
const copyButton = document.getElementById("copy-demo-key");
const status = document.getElementById("demo-key-status");
const result = document.getElementById("demo-key-result");
const keyInput = document.getElementById("demo-api-key");
const walletId = document.getElementById("demo-wallet-id");
const budgetCredits = document.getElementById("demo-budget-credits");
const commands = document.getElementById("demo-commands");
const commandTemplate = commands.textContent;

const UTM_PARAMS = ["utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term"];
const UTM_STORAGE_KEY = "amwDemoKeyUtm";
const UTM_MAX_LENGTH = 128;
const REFERRER_MAX_LENGTH = 512;

function shellQuote(value) {
  return "'" + value.replace(/'/g, "'\"'\"'") + "'";
}

// Same record as /utm-capture.js. Repeated here so a direct visit still
// attributes when that script never ran (standalone copy of this page).
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
    // Storage can be blocked; the request still sends this page's values.
  }
}

function attributionPayload() {
  const record = captureAttribution();
  persistAttribution(record);
  const payload = {};
  for (const key of UTM_PARAMS) {
    if (record[key]) payload[key] = record[key];
  }
  // A same-origin referrer is not stored (it would hide the first external
  // one), but a direct visit with only that referrer still reports it.
  const referrer = record.referrer || pageReferrer();
  if (referrer) payload.referrer = referrer;
  return payload;
}

persistAttribution(captureAttribution());

issueButton.addEventListener("click", async () => {
  issueButton.disabled = true;
  status.textContent = "Requesting a demo key…";

  const attribution = attributionPayload();
  const request = {
    method: "POST",
    credentials: "omit",
  };
  if (Object.keys(attribution).length > 0) {
    request.headers = { "Content-Type": "application/json" };
    request.body = JSON.stringify(attribution);
  }

  let response;
  try {
    response = await fetch("https://api.thisisatest.tech/v1/demo/keys", request);
  } catch {
    status.textContent = "Could not reach the demo API. Check your connection and try again.";
    issueButton.disabled = false;
    return;
  }

  if (response.status !== 201) {
    const messages = {
      403: "This page is not allowed to issue demo keys right now. Try the curl fallback below.",
      404: "The demo is off right now. Please try again later.",
      429: "Demo key requests are rate limited. Please try again later.",
      503: "Demo key issuance is temporarily unavailable. Please try again later.",
    };
    status.textContent = messages[response.status] || "Could not issue a demo key. Please try again later.";
    issueButton.disabled = false;
    return;
  }

  let data;
  try {
    data = await response.json();
  } catch {
    status.textContent = "The demo API returned an unreadable response. Please try again later.";
    issueButton.disabled = false;
    return;
  }
  if (
    !data ||
    typeof data.api_key !== "string" || !data.api_key ||
    typeof data.wallet_id !== "string" || !data.wallet_id ||
    !["number", "string"].includes(typeof data.budget_credits)
  ) {
    status.textContent = "The demo API did not return a usable key. Please try again later.";
    issueButton.disabled = false;
    return;
  }

  keyInput.value = data.api_key;
  walletId.textContent = data.wallet_id;
  budgetCredits.textContent = String(data.budget_credits);
  commands.textContent = commandTemplate
    .replace("KEY='YOUR_DEMO_KEY'", `KEY=${shellQuote(data.api_key)}`)
    .replace("WALLET='YOUR_WALLET_ID'", `WALLET=${shellQuote(data.wallet_id)}`);
  result.hidden = false;
  status.textContent = "Demo key ready. Copy and store it now.";
});

copyButton.addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText(keyInput.value);
    status.textContent = "Demo key copied.";
  } catch {
    keyInput.focus();
    keyInput.select();
    status.textContent = "Copy was unavailable. The key is selected so you can copy it manually.";
  }
});
})();
