import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { createContext, runInContext } from "node:vm";

const script = readFileSync(new URL("./home-key.js", import.meta.url), "utf8");
const captureScript = readFileSync(new URL("./utm-capture.js", import.meta.url), "utf8");
const homeHtml = readFileSync(new URL("./index.html", import.meta.url), "utf8");
const heroCode = homeHtml.match(/<pre id="home-hero-python"[^>]*><code>([\s\S]*?)<\/code><\/pre>/);
assert.ok(heroCode, "hero Python command block must exist");
const heroTemplate = heroCode[1].replaceAll("&amp;", "&");
const template = `git clone https://github.com/agentmiddleware/amw-demo.git
cd amw-demo
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
export AMW_API_KEY='YOUR_DEMO_KEY'
export AMW_WALLET_ID='YOUR_WALLET_ID'
python examples/permit_echo_retry.py`;

function makePage(fetchResponse, { copyFails = false, capture = true, search = "", referrer = "", session: stored = {} } = {}) {
  const ids = [
    "home-key-trigger", "home-key-panel", "home-key-close", "home-key-status",
    "home-key-title", "home-key-result", "home-key-error", "home-key-error-message",
    "home-key-value", "home-wallet-value", "home-key-reveal", "home-wallet-reveal",
    "home-key-copy", "home-wallet-copy", "home-key-copy-both", "home-key-budget",
    "home-key-retry", "home-key-email", "home-quickstart-code",
    "home-copy-quickstart", "home-copy-status",
    "home-hero-python-copy", "home-hero-curl-copy", "home-hero-python",
    "home-hero-curl", "home-hero-copy-status",
  ];
  const elements = new Map(ids.map((id) => [id, {
    id,
    hidden: ["home-key-trigger", "home-key-panel", "home-key-result", "home-key-error",
      "home-key-retry", "home-key-email"].includes(id),
    disabled: id === "home-copy-quickstart" || id === "home-hero-python-copy",
    textContent: id === "home-quickstart-code" ? template :
      id === "home-hero-python" ? heroTemplate :
      id === "home-hero-curl" ? "curl -s -X POST https://api.thisisatest.tech/v1/demo/keys" :
      id === "home-key-trigger" ? "Get a free demo key" : "",
    value: "",
    type: id === "home-key-value" || id === "home-wallet-value" ? "password" : "button",
    attributes: {},
    listeners: {},
    focusCount: 0,
    selectCount: 0,
    addEventListener(event, listener) { this.listeners[event] = listener; },
    setAttribute(name, value) { this.attributes[name] = value; },
    focus() { if (!this.disabled && !this.hidden) this.focusCount += 1; },
    select() { this.selectCount += 1; },
    set innerHTML(_) { throw new Error("API data must not be inserted as HTML"); },
  }]));
  elements.get("home-quickstart-code").select = undefined;
  const requests = [];
  const copies = [];
  const session = new Map(Object.entries(stored));
  const forbidden = () => { throw new Error("key must not be stored or logged"); };
  const context = createContext({
    document: {
      referrer,
      getElementById(id) { return elements.get(id); },
      set cookie(_) { forbidden(); },
    },
    location: { search, origin: "https://www.agentmiddleware.dev" },
    URL,
    URLSearchParams,
    async fetch(url, options) {
      requests.push([url, options]);
      return fetchResponse();
    },
    navigator: {
      clipboard: {
        async writeText(value) {
          if (copyFails) throw new Error("clipboard blocked");
          copies.push(value);
        },
      },
    },
    localStorage: { getItem: forbidden, setItem: forbidden },
    sessionStorage: {
      getItem(key) { return session.has(key) ? session.get(key) : null; },
      setItem(key, value) { session.set(key, String(value)); },
    },
    console: { log: forbidden, warn: forbidden, error: forbidden, info: forbidden, debug: forbidden, table: forbidden },
    window: { va: forbidden },
  });
  if (capture) runInContext(captureScript, context, { filename: "utm-capture.js" });
  runInContext(script, context, { filename: "home-key.js" });
  return {
    elements,
    requests,
    copies,
    session,
    async click(id) {
      const element = elements.get(id);
      if (element.disabled || element.hidden) return;
      return element.listeners.click({ currentTarget: element });
    },
  };
}

function issuedResponse(apiKey = "b2a_" + "test", walletId = "agt-test", credits = "1000") {
  return {
    status: 201,
    async json() {
      return { api_key: apiKey, wallet_id: walletId, budget_credits: credits };
    },
  };
}

test("hero Python copy requires an issued demo key and wallet", async () => {
  assert.match(heroTemplate, /export AMW_API_KEY='YOUR_DEMO_KEY'\nexport AMW_WALLET_ID='YOUR_WALLET_ID'\npython examples\/permit_echo_retry\.py$/);
  assert.match(homeHtml, /id="home-hero-python-copy"[^>]*disabled/);
  const page = makePage(() => { throw new Error("unexpected key request"); });
  await page.click("home-hero-python-copy");
  await page.click("home-hero-curl-copy");
  assert.deepEqual(page.copies, [
    "curl -s -X POST https://api.thisisatest.tech/v1/demo/keys",
  ]);
  assert.equal(page.elements.get("home-hero-copy-status").textContent, "Commands copied.");
  assert.equal(page.requests.length, 0);
});

test("idle, loading, and success issue one empty POST and fill the live quickstart", async () => {
  let resolveResponse;
  const page = makePage(() => new Promise((resolve) => { resolveResponse = resolve; }));
  const get = (id) => page.elements.get(id);
  assert.equal(get("home-key-trigger").hidden, false);
  assert.equal(get("home-key-panel").hidden, true);
  assert.equal(get("home-copy-quickstart").disabled, true);
  assert.equal(get("home-hero-python-copy").disabled, true);
  assert.equal(page.requests.length, 0);

  const pending = page.click("home-key-trigger");
  assert.equal(get("home-key-panel").hidden, false);
  assert.equal(get("home-key-trigger").disabled, true);
  assert.equal(get("home-key-trigger").textContent, "Getting your key");
  assert.equal(get("home-key-status").textContent, "Getting your key");
  await get("home-key-retry").listeners.click();
  assert.equal(page.requests.length, 1);
  const [url, request] = page.requests[0];
  assert.equal(url, "https://api.thisisatest.tech/v1/demo/keys");
  assert.equal(request.method, "POST");
  assert.equal(request.credentials, "omit");
  assert.deepEqual(Object.keys(request).sort(), ["credentials", "method"]);

  const key = "b2a_test_'$&<img>";
  resolveResponse(issuedResponse(key, "agt-test", "1000"));
  await pending;
  assert.equal(get("home-key-title").textContent, "Your demo key is ready");
  assert.equal(get("home-key-status").textContent, "Your demo key is ready");
  assert.equal(get("home-key-panel").focusCount, 1);
  assert.equal(get("home-key-result").hidden, false);
  assert.equal(get("home-key-error").hidden, true);
  assert.equal(get("home-key-value").type, "password");
  assert.equal(get("home-wallet-value").type, "password");
  assert.equal(get("home-key-value").value, key);
  assert.equal(get("home-wallet-value").value, "agt-test");
  assert.equal(get("home-key-budget").textContent, "1000");
  assert.equal(get("home-copy-quickstart").disabled, false);
  assert.equal(get("home-hero-python-copy").disabled, false);
  assert.equal(get("home-quickstart-code").textContent,
    template.replace("export AMW_API_KEY='YOUR_DEMO_KEY'", () => "export AMW_API_KEY='b2a_test_'\"'\"'$&<img>'")
      .replace("export AMW_WALLET_ID='YOUR_WALLET_ID'", "export AMW_WALLET_ID='agt-test'"));
  assert.equal(get("home-hero-python").textContent,
    heroTemplate.replace("export AMW_API_KEY='YOUR_DEMO_KEY'", () => "export AMW_API_KEY='b2a_test_'\"'\"'$&<img>'")
      .replace("export AMW_WALLET_ID='YOUR_WALLET_ID'", "export AMW_WALLET_ID='agt-test'"));
  await page.click("home-hero-python-copy");
  assert.equal(page.copies[0], get("home-hero-python").textContent);
  assert.match(page.copies[0], /export AMW_WALLET_ID='agt-test'\npython examples\/permit_echo_retry\.py$/);
  assert.equal(page.copies[0].includes("YOUR_DEMO_KEY"), false);
  assert.equal(get("home-key-trigger").disabled, false);
  await page.click("home-key-close");
  assert.equal(get("home-key-panel").hidden, true);
  assert.equal(get("home-key-trigger").focusCount, 1);
  await page.click("home-key-trigger");
  assert.equal(get("home-key-panel").hidden, false);
  assert.equal(page.requests.length, 1);
});

test("inline issuance sends UTM and referrer without storing the issued key", async () => {
  const issuedKey = "not a real key";
  const page = makePage(() => issuedResponse(issuedKey), {
    search: "?utm_source=newsletter&utm_medium=%20&utm_term=receipts",
    referrer: " https://news.example/item ",
    session: {
      amwDemoKeyUtm: JSON.stringify({ utm_source: "old-source", utm_campaign: "launch" }),
    },
  });
  const expected = {
    utm_source: "newsletter",
    utm_campaign: "launch",
    utm_term: "receipts",
    referrer: "https://news.example/item",
  };
  await page.click("home-key-trigger");
  assert.equal(page.requests.length, 1);
  const [, request] = page.requests[0];
  assert.equal(request.headers["Content-Type"], "application/json");
  assert.deepEqual(JSON.parse(request.body), expected);
  assert.deepEqual(JSON.parse(page.session.get("amwDemoKeyUtm")), expected);
  assert.equal(page.elements.get("home-key-value").value, issuedKey);
  assert.equal(request.body.includes(issuedKey), false);
  assert.equal([...page.session.values()].some((value) => value.includes(issuedKey)), false);

  const sameOrigin = makePage(() => issuedResponse(), {
    referrer: "https://www.agentmiddleware.dev/docs",
  });
  await sameOrigin.click("home-key-trigger");
  assert.deepEqual(JSON.parse(sameOrigin.requests[0][1].body), {
    referrer: "https://www.agentmiddleware.dev/docs",
  });
  assert.equal(sameOrigin.session.size, 0);
});

test("issuance still works if the attribution script does not load", async () => {
  const page = makePage(() => issuedResponse(), { capture: false });
  await page.click("home-key-trigger");
  assert.equal(page.requests.length, 1);
  assert.deepEqual(Object.keys(page.requests[0][1]).sort(), ["credentials", "method"]);
  assert.equal(page.elements.get("home-key-result").hidden, false);
});

test("reveal and copy controls use full values and quote shell commands", async () => {
  const page = makePage(() => issuedResponse("b2a_a'b", "agt-w"));
  const get = (id) => page.elements.get(id);
  await page.click("home-key-trigger");
  await page.click("home-key-reveal");
  assert.equal(get("home-key-value").type, "text");
  assert.equal(get("home-key-reveal").attributes["aria-pressed"], "true");
  assert.equal(get("home-key-reveal").attributes["aria-label"], "Hide key");
  await page.click("home-key-reveal");
  assert.equal(get("home-key-value").type, "password");
  assert.equal(get("home-key-reveal").attributes["aria-label"], "Reveal key");
  await page.click("home-wallet-reveal");
  assert.equal(get("home-wallet-value").type, "text");
  assert.equal(get("home-wallet-reveal").attributes["aria-label"], "Hide wallet");
  await page.click("home-key-copy");
  await page.click("home-wallet-copy");
  await page.click("home-key-copy-both");
  await page.click("home-copy-quickstart");
  assert.deepEqual(page.copies, [
    "b2a_a'b",
    "agt-w",
    "export AMW_API_KEY='b2a_a'\"'\"'b'\nexport AMW_WALLET_ID='agt-w'",
    get("home-quickstart-code").textContent,
  ]);
  assert.equal(get("home-copy-status").textContent, "Quickstart copied.");
});

test("network and service failures show retry and a later request can succeed", async () => {
  for (const fail of [() => { throw new Error("offline"); }, () => ({ status: 503 })]) {
    let count = 0;
    const page = makePage(() => count++ === 0 ? fail() : issuedResponse());
    const get = (id) => page.elements.get(id);
    await page.click("home-key-trigger");
    assert.equal(get("home-key-status").textContent,
      "We could not create a key right now. Try again in a minute.");
    assert.equal(get("home-key-error-message").textContent,
      "We could not create a key right now. Try again in a minute.");
    assert.equal(get("home-key-error").hidden, false);
    assert.equal(get("home-key-retry").hidden, false);
    assert.equal(get("home-key-email").hidden, true);
    assert.equal(get("home-key-result").hidden, true);
    await page.click("home-key-retry");
    assert.equal(page.requests.length, 2);
    assert.equal(get("home-key-result").hidden, false);
  }
});

test("failed issuance focuses the visible action when open or trigger when closed", async () => {
  for (const [status, action] of [[503, "home-key-retry"], [429, "home-key-email"]]) {
    const page = makePage(() => ({ status }));
    const get = (id) => page.elements.get(id);
    await page.click("home-key-trigger");
    assert.equal(get("home-key-panel").hidden, false);
    assert.equal(get(action).hidden, false);
    assert.equal(get(action).focusCount, 1);
    assert.equal(get("home-key-trigger").focusCount, 0);
  }

  let resolveResponse;
  const page = makePage(() => new Promise((resolve) => { resolveResponse = resolve; }));
  const pending = page.click("home-key-trigger");
  await page.click("home-key-close");
  resolveResponse({ status: 503 });
  await pending;
  assert.equal(page.elements.get("home-key-panel").hidden, true);
  assert.equal(page.elements.get("home-key-trigger").focusCount, 1);
  assert.equal(page.elements.get("home-key-retry").focusCount, 0);
});

test("rate limit and disabled responses show email without another issuance", async () => {
  for (const code of [403, 404, 429]) {
    const page = makePage(() => ({ status: code }));
    const get = (id) => page.elements.get(id);
    await page.click("home-key-trigger");
    assert.equal(get("home-key-status").textContent,
      "Demo keys are paused right now. Email us and we will send one.");
    assert.equal(get("home-key-error-message").textContent,
      "Demo keys are paused right now. Email us and we will send one.");
    assert.equal(get("home-key-error").hidden, false);
    assert.equal(get("home-key-retry").hidden, true);
    assert.equal(get("home-key-email").hidden, false);
    await page.click("home-key-close");
    await page.click("home-key-trigger");
    assert.equal(page.requests.length, 1);
  }
});

test("malformed success fails closed and unavailable clipboard focuses a manual fallback", async () => {
  const invalidResponses = [
    { status: 201, async json() { return { api_key: "b2a_x" }; } },
    { status: 201, async json() { throw new Error("invalid JSON"); } },
    issuedResponse(" b2a_x"),
  ];
  for (const response of invalidResponses) {
    const invalid = makePage(() => response);
    await invalid.click("home-key-trigger");
    assert.equal(invalid.elements.get("home-key-result").hidden, true);
    assert.equal(invalid.elements.get("home-key-retry").hidden, false);
    assert.equal(invalid.elements.get("home-copy-quickstart").disabled, true);
    assert.equal(invalid.elements.get("home-hero-python-copy").disabled, true);
  }

  const page = makePage(() => issuedResponse(), { copyFails: true });
  await page.click("home-key-trigger");
  await page.click("home-key-copy");
  assert.equal(page.elements.get("home-key-value").focusCount, 1);
  assert.equal(page.elements.get("home-key-value").selectCount, 1);
  await page.click("home-key-copy-both");
  assert.equal(page.elements.get("home-key-status").textContent,
    "Copy was unavailable. Copy Key and Wallet separately.");
  await page.click("home-copy-quickstart");
  assert.equal(page.elements.get("home-quickstart-code").focusCount, 1);
  assert.equal(page.elements.get("home-copy-status").textContent,
    "Copy was unavailable. Select the text and copy it manually.");
});

test("closing during issuance keeps the drawer closed until the visitor reopens it", async () => {
  let resolveResponse;
  const page = makePage(() => new Promise((resolve) => { resolveResponse = resolve; }));
  const pending = page.click("home-key-trigger");
  await page.click("home-key-close");
  resolveResponse(issuedResponse());
  await pending;
  assert.equal(page.elements.get("home-key-panel").hidden, true);
  assert.equal(page.elements.get("home-key-trigger").focusCount, 1);
  await page.click("home-key-trigger");
  assert.equal(page.elements.get("home-key-panel").hidden, false);
  assert.equal(page.requests.length, 1);
});
