(function () {
  const issueButton = document.getElementById("get-try-key");
  const status = document.getElementById("try-key-status");
  const result = document.getElementById("try-key-result");
  const keyInput = document.getElementById("try-api-key");
  const walletInput = document.getElementById("try-wallet-id");
  const emailInput = document.getElementById("try-email");
  const emailError = document.getElementById("try-email-error");
  const UTM_PARAMS = ["utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term"];
  const UTM_STORAGE_KEY = "amwDemoKeyUtm";
  const UTM_MAX_LENGTH = 128;
  const REFERRER_MAX_LENGTH = 512;

  // Match demo-key.js so direct visits still work if utm-capture.js did not run.
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
      // Storage can be blocked; this page still sends its own attribution.
    }
  }

  // utm-capture.js maintains the same record across pages. This mirrors
  // demo-key.js at issue time, including the same-origin referrer fallback.
  function collectSignupFields() {
    const record = captureAttribution();
    persistAttribution(record);
    const fields = {};
    for (const key of UTM_PARAMS) {
      if (record[key]) fields[key] = record[key];
    }
    const referrer = record.referrer || pageReferrer();
    if (referrer) fields.referrer = referrer;
    return fields;
  }

  persistAttribution(captureAttribution());

  async function onKeyIssued({ wallet_id, fields }) {
    const signup = { wallet_id };
    for (const key of UTM_PARAMS) {
      if (fields[key]) signup[key] = fields[key];
    }
    if (fields.referrer) signup.referrer = fields.referrer;

    const email = emailInput.value.trim();
    if (email) {
      if (email.length <= 254 && /^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email)) {
        signup.email = email;
      } else {
        emailError.textContent = "That email address was not included. Your test key is ready.";
        emailError.hidden = false;
      }
    }

    try {
      await fetch("https://cqtlzicxmaklslvxderm.supabase.co/functions/v1/amw-test-signup", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "omit",
        keepalive: true,
        body: JSON.stringify(signup),
      });
    } catch {
      // Signup is optional and never interrupts key issuance.
    }
  }

  issueButton.addEventListener("click", async () => {
    issueButton.disabled = true;
    status.textContent = "Requesting a test key…";

    const fields = collectSignupFields();
    const request = { method: "POST", credentials: "omit" };
    if (Object.keys(fields).length > 0) {
      request.headers = { "Content-Type": "application/json" };
      request.body = JSON.stringify(fields);
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
        403: "This page cannot issue test keys right now. Please try again later.",
        404: "The demo is off right now. Please try again later.",
        429: "Test key requests are rate limited. Please try again later.",
        503: "Test key issuance is temporarily unavailable. Please try again later.",
      };
      status.textContent = messages[response.status] || "Could not issue a test key. Please try again later.";
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
    walletInput.value = data.wallet_id;
    result.hidden = false;
    status.textContent = "Test key ready. Copy it now.";
    void onKeyIssued({ wallet_id: data.wallet_id, fields });
  });

  async function copyValue(input, label) {
    try {
      await navigator.clipboard.writeText(input.value);
      status.textContent = `${label} copied.`;
    } catch {
      input.focus();
      input.select();
      status.textContent = `Copy was unavailable. The ${label.toLowerCase()} is selected so you can copy it manually.`;
    }
  }

  document.getElementById("copy-try-key").addEventListener("click", () => copyValue(keyInput, "API key"));
  document.getElementById("copy-try-wallet").addEventListener("click", () => copyValue(walletInput, "Wallet ID"));
})();
