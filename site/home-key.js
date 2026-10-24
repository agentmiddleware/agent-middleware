(function () {
  const byId = (id) => document.getElementById(id);
  const trigger = byId("home-key-trigger");
  const panel = byId("home-key-panel");
  const close = byId("home-key-close");
  const status = byId("home-key-status");
  const title = byId("home-key-title");
  const result = byId("home-key-result");
  const error = byId("home-key-error");
  const errorMessage = byId("home-key-error-message");
  const key = byId("home-key-value");
  const wallet = byId("home-wallet-value");
  const budget = byId("home-key-budget");
  const retry = byId("home-key-retry");
  const email = byId("home-key-email");
  const quickstart = byId("home-quickstart-code");
  const copyQuickstart = byId("home-copy-quickstart");
  const copyStatus = byId("home-copy-status");
  const quickstartTemplate = quickstart.textContent;
  const heroQuickstart = byId("home-hero-python");
  const heroQuickstartTemplate = heroQuickstart.textContent;
  const copyHeroQuickstart = byId("home-hero-python-copy");
  const triggerLabel = trigger.textContent;
  let pending = false;
  let issued = false;
  let paused = false;

  function shellQuote(value) {
    return "'" + value.replace(/'/g, "'\"'\"'") + "'";
  }

  function fillQuickstart(template, data) {
    return template
      .replace("export AMW_API_KEY='YOUR_DEMO_KEY'", () =>
        `export AMW_API_KEY=${shellQuote(data.api_key)}`)
      .replace("export AMW_WALLET_ID='YOUR_WALLET_ID'", () =>
        `export AMW_WALLET_ID=${shellQuote(data.wallet_id)}`);
  }

  function showPanel() {
    panel.hidden = false;
  }

  function finishRequest() {
    pending = false;
    trigger.disabled = false;
    trigger.textContent = triggerLabel;
  }

  function showFailure(isPaused) {
    paused = isPaused;
    title.textContent = isPaused ? "Demo keys are paused" : "Could not create a demo key";
    const message = isPaused
      ? "Demo keys are paused right now. Email us and we will send one."
      : "We could not create a key right now. Try again in a minute.";
    status.textContent = message;
    errorMessage.textContent = message;
    result.hidden = true;
    error.hidden = false;
    retry.hidden = isPaused;
    email.hidden = !isPaused;
    finishRequest();
    if (panel.hidden) trigger.focus();
    else (isPaused ? email : retry).focus();
  }

  function usableIssue(data) {
    const credits = data && data.budget_credits;
    return data &&
      typeof data.api_key === "string" && data.api_key.length > 0 &&
      data.api_key === data.api_key.trim() && !/[\x00-\x1f\x7f]/.test(data.api_key) &&
      typeof data.wallet_id === "string" && data.wallet_id.length > 0 &&
      data.wallet_id === data.wallet_id.trim() && !/[\x00-\x1f\x7f]/.test(data.wallet_id) &&
      ((typeof credits === "number" && Number.isFinite(credits) && credits >= 0) ||
        (typeof credits === "string" && /^\d+(?:\.\d+)?$/.test(credits)));
  }

  function showSuccess(data) {
    issued = true;
    key.value = data.api_key;
    wallet.value = data.wallet_id;
    key.type = "password";
    wallet.type = "password";
    budget.textContent = String(data.budget_credits);
    quickstart.textContent = fillQuickstart(quickstartTemplate, data);
    heroQuickstart.textContent = fillQuickstart(heroQuickstartTemplate, data);
    copyQuickstart.disabled = false;
    copyHeroQuickstart.disabled = false;
    title.textContent = "Your demo key is ready";
    status.textContent = "Your demo key is ready";
    result.hidden = false;
    error.hidden = true;
    finishRequest();
    if (panel.hidden) trigger.focus();
    else panel.focus();
  }

  async function requestKey() {
    if (pending) return;
    showPanel();
    if (issued || paused) {
      panel.focus();
      return;
    }
    pending = true;
    trigger.disabled = true;
    trigger.textContent = "Getting your key";
    title.textContent = "Getting your key";
    status.textContent = "Getting your key";
    result.hidden = true;
    error.hidden = true;

    const attribution = typeof captureAttribution === "function" ? captureAttribution() : {};
    if (typeof persistAttribution === "function") persistAttribution(attribution);
    const referrer = attribution.referrer ||
      (typeof pageReferrer === "function" ? pageReferrer() : "");
    if (referrer) attribution.referrer = referrer;
    const request = { method: "POST", credentials: "omit" };
    if (Object.keys(attribution).length > 0) {
      request.headers = { "Content-Type": "application/json" };
      request.body = JSON.stringify(attribution);
    }

    let response;
    try {
      response = await fetch("https://api.thisisatest.tech/v1/demo/keys", request);
    } catch {
      showFailure(false);
      return;
    }
    if (response.status === 404 || response.status === 429 || response.status === 403) {
      showFailure(true);
      return;
    }
    if (response.status !== 201) {
      showFailure(false);
      return;
    }
    let data;
    try {
      data = await response.json();
    } catch {
      showFailure(false);
      return;
    }
    if (!usableIssue(data)) {
      showFailure(false);
      return;
    }
    showSuccess(data);
  }

  async function copy(value, notice, fallback, target = status, unavailable = "Copy was unavailable. Select the text and copy it manually.") {
    try {
      await navigator.clipboard.writeText(value);
      target.textContent = notice;
    } catch {
      fallback.focus();
      if (typeof fallback.select === "function") fallback.select();
      target.textContent = unavailable;
    }
  }

  function toggleReveal(input, button, label) {
    const revealed = input.type === "password";
    input.type = revealed ? "text" : "password";
    button.textContent = revealed ? "Hide" : "Reveal";
    button.setAttribute("aria-label", `${revealed ? "Hide" : "Reveal"} ${label}`);
    button.setAttribute("aria-pressed", String(revealed));
  }

  trigger.hidden = false;
  trigger.addEventListener("click", requestKey);
  retry.addEventListener("click", requestKey);
  close.addEventListener("click", () => {
    panel.hidden = true;
    trigger.focus();
  });
  byId("home-key-reveal").addEventListener("click", (event) =>
    toggleReveal(key, event.currentTarget, "key"));
  byId("home-wallet-reveal").addEventListener("click", (event) =>
    toggleReveal(wallet, event.currentTarget, "wallet"));
  byId("home-key-copy").addEventListener("click", () =>
    copy(key.value, "Key copied.", key));
  byId("home-wallet-copy").addEventListener("click", () =>
    copy(wallet.value, "Wallet copied.", wallet));
  byId("home-key-copy-both").addEventListener("click", () =>
    copy(`export AMW_API_KEY=${shellQuote(key.value)}\nexport AMW_WALLET_ID=${shellQuote(wallet.value)}`,
      "Key and wallet commands copied.", key, status,
      "Copy was unavailable. Copy Key and Wallet separately."));
  copyQuickstart.addEventListener("click", () =>
    copy(quickstart.textContent, "Quickstart copied.", quickstart, copyStatus));
  for (const [buttonId, codeId] of [
    ["home-hero-python-copy", "home-hero-python"],
    ["home-hero-curl-copy", "home-hero-curl"],
  ]) {
    const button = byId(buttonId);
    const code = byId(codeId);
    if (button && code) {
      button.addEventListener("click", () =>
        copy(code.textContent, "Commands copied.", code, byId("home-hero-copy-status")));
    }
  }
})();
