import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { createContext, runInContext, runInNewContext } from "node:vm";

const script = readFileSync(new URL("./demo-key/demo-key.js", import.meta.url), "utf8");
const captureScript = readFileSync(new URL("./utm-capture.js", import.meta.url), "utf8");
const commandTemplate = `API=https://api.thisisatest.tech
KEY='YOUR_DEMO_KEY'
WALLET='YOUR_WALLET_ID'
curl -fsS "$API/v1/permits"
curl -fsS "$API/mcp/tools/partner.echo/invoke"
curl -fsS "$API/v1/receipts/$RECEIPT/portable"`;

function makePage(fetchResponse, options = {}) {
  const ids = [
    "get-demo-key",
    "copy-demo-key",
    "demo-key-status",
    "demo-key-result",
    "demo-api-key",
    "demo-wallet-id",
    "demo-budget-credits",
    "demo-commands",
  ];
  const elements = new Map(ids.map((id) => [id, {
    disabled: false,
    hidden: true,
    textContent: id === "demo-commands" ? commandTemplate : "",
    value: "",
    listeners: {},
    addEventListener(event, listener) { this.listeners[event] = listener; },
    set innerHTML(_) { throw new Error("API data must not be inserted as HTML"); },
  }]));
  const requests = [];
  const copies = [];
  const session = new Map(Object.entries(options.session || {}));
  const forbidden = () => { throw new Error("key must not be persisted or logged"); };
  const document = {
    referrer: options.referrer || "",
    getElementById(id) { return elements.get(id); },
    set cookie(_) { forbidden(); },
  };
  runInNewContext(script, {
    document,
    location: {
      search: options.search || "",
      origin: options.origin || "https://www.agentmiddleware.dev",
    },
    URLSearchParams,
    URL,
    async fetch(url, options) {
      requests.push([url, options]);
      return fetchResponse();
    },
    navigator: { clipboard: { async writeText(value) { copies.push(value); } } },
    localStorage: { setItem: forbidden, getItem: forbidden },
    sessionStorage: {
      getItem(key) { return session.has(key) ? session.get(key) : null; },
      setItem(key, value) { session.set(key, String(value)); },
    },
    console: { log: forbidden },
  }, { filename: "demo-key.js" });
  return {
    elements,
    requests,
    copies,
    session,
    async click(id) {
      const element = elements.get(id);
      if (!element.disabled) await element.listeners.click();
    },
  };
}

test("issuance uses an empty credential-free POST and renders one key as text", async () => {
  const key = "demo_<img src=x onerror=alert(1)>";
  const page = makePage(async () => ({
    status: 201,
    async json() {
      return { api_key: key, wallet_id: "wallet_demo_1", budget_credits: 25 };
    },
  }));

  await page.click("get-demo-key");
  assert.equal(page.requests.length, 1);
  const [url, options] = page.requests[0];
  assert.equal(url, "https://api.thisisatest.tech/v1/demo/keys");
  assert.equal(options.method, "POST");
  assert.equal(options.credentials, "omit");
  assert.deepEqual(Object.keys(options).sort(), ["credentials", "method"]);
  assert.equal(page.elements.get("get-demo-key").disabled, true);
  assert.equal(page.elements.get("demo-key-result").hidden, false);
  assert.equal(page.elements.get("demo-api-key").value, key);
  assert.equal(page.elements.get("demo-wallet-id").textContent, "wallet_demo_1");
  assert.equal(page.elements.get("demo-budget-credits").textContent, "25");
  assert.equal(page.elements.get("demo-commands").textContent,
    commandTemplate.replace("KEY='YOUR_DEMO_KEY'", `KEY='${key}'`)
      .replace("WALLET='YOUR_WALLET_ID'", "WALLET='wallet_demo_1'"));
  await page.click("copy-demo-key");
  assert.deepEqual(page.copies, [key]);
  await page.click("get-demo-key");
  assert.equal(page.requests.length, 1);
  assert.equal(page.session.size, 0);
  assert.equal([...page.session.values()].some((value) => value.includes(key)), false);
});

test("issuance sends stored UTM overridden by the page URL plus referrer", async () => {
  const key = "not a real key";
  const page = makePage(async () => ({
    status: 201,
    async json() {
      return { api_key: key, wallet_id: "wallet_demo_1", budget_credits: 25 };
    },
  }), {
    search: "?utm_source=newsletter&utm_medium=%20&utm_term=receipts",
    referrer: " https://news.example/item ",
    session: {
      amwDemoKeyUtm: JSON.stringify({
        utm_source: "old-source",
        utm_campaign: "launch",
        utm_content: "  ",
      }),
    },
  });

  const storedBeforeClick = JSON.parse(page.session.get("amwDemoKeyUtm"));
  assert.deepEqual(storedBeforeClick, {
    utm_source: "newsletter",
    utm_campaign: "launch",
    utm_term: "receipts",
    referrer: "https://news.example/item",
  });

  await page.click("get-demo-key");
  const [, request] = page.requests[0];
  assert.equal(request.headers["Content-Type"], "application/json");
  assert.deepEqual(JSON.parse(request.body), {
    utm_source: "newsletter",
    utm_campaign: "launch",
    utm_term: "receipts",
    referrer: "https://news.example/item",
  });
  assert.deepEqual(JSON.parse(page.session.get("amwDemoKeyUtm")), {
    utm_source: "newsletter",
    utm_campaign: "launch",
    utm_term: "receipts",
    referrer: "https://news.example/item",
  });
  assert.equal([...page.session.values()].some((value) => value.includes(key)), false);
});

test("overlong UTM and referrer are capped before storage and before POST", async () => {
  const page = makePage(async () => ({
    status: 201,
    async json() {
      return { api_key: "not a real key", wallet_id: "wallet_demo_1", budget_credits: 25 };
    },
  }), {
    search: `?utm_source=${encodeURIComponent(`  ${"s".repeat(140)}  `)}&utm_campaign=${"c".repeat(200)}&utm_medium=%20%20`,
    referrer: ` https://news.example/${"r".repeat(600)} `,
  });

  const stored = JSON.parse(page.session.get("amwDemoKeyUtm"));
  assert.equal(stored.utm_source, "s".repeat(128));
  assert.equal(stored.utm_campaign, "c".repeat(128));
  assert.equal("utm_medium" in stored, false);
  assert.equal(stored.referrer.length, 512);
  assert.equal(stored.referrer, `https://news.example/${"r".repeat(600)}`.slice(0, 512));
  assert.equal([...page.session.values()].some((value) => value.includes("c".repeat(129))), false);

  await page.click("get-demo-key");
  const [, request] = page.requests[0];
  assert.deepEqual(JSON.parse(request.body), stored);
});

test("the first external referrer sticks and a same-origin referrer is only posted", async () => {
  const issued = async () => ({
    status: 201,
    async json() {
      return { api_key: "not a real key", wallet_id: "wallet_demo_1", budget_credits: 25 };
    },
  });
  const kept = makePage(issued, {
    referrer: "https://www.agentmiddleware.dev/compare/",
    session: {
      amwDemoKeyUtm: JSON.stringify({
        utm_source: "newsletter",
        referrer: `https://first.example/${"p".repeat(600)}`,
      }),
    },
  });
  const stored = JSON.parse(kept.session.get("amwDemoKeyUtm"));
  assert.equal(stored.referrer, `https://first.example/${"p".repeat(600)}`.slice(0, 512));
  await kept.click("get-demo-key");
  assert.equal(JSON.parse(kept.requests[0][1].body).referrer, stored.referrer);

  const sameOrigin = makePage(issued, {
    referrer: " https://www.agentmiddleware.dev/ ",
  });
  assert.equal(sameOrigin.session.size, 0);
  await sameOrigin.click("get-demo-key");
  assert.deepEqual(JSON.parse(sameOrigin.requests[0][1].body), {
    referrer: "https://www.agentmiddleware.dev/",
  });
  assert.equal(sameOrigin.session.size, 0);
});

test("issuance failures show actionable messages and allow retry", async () => {
  const messages = new Map([
    [403, "This page is not allowed to issue demo keys right now. Try the curl fallback below."],
    [404, "The demo is off right now. Please try again later."],
    [429, "Demo key requests are rate limited. Please try again later."],
    [503, "Demo key issuance is temporarily unavailable. Please try again later."],
  ]);
  for (const [code, message] of messages) {
    const page = makePage(async () => ({ status: code }));
    await page.click("get-demo-key");
    assert.equal(page.elements.get("demo-key-status").textContent, message);
    assert.equal(page.elements.get("get-demo-key").disabled, false);
    assert.equal(page.elements.get("demo-key-result").hidden, true);
  }

  const network = makePage(async () => { throw new Error("offline"); });
  await network.click("get-demo-key");
  assert.equal(network.elements.get("demo-key-status").textContent,
    "Could not reach the demo API. Check your connection and try again.");
  assert.equal(network.elements.get("get-demo-key").disabled, false);
});

test("utm-capture.js and demo-key.js share one classic-script global", () => {
  const session = new Map();
  const listeners = {};
  const context = createContext({
    document: {
      referrer: "https://news.example/item",
      getElementById(id) {
        return {
          disabled: false,
          hidden: true,
          textContent: id === "demo-commands" ? commandTemplate : "",
          value: "",
          addEventListener(event, listener) { listeners[id] = listener; },
        };
      },
    },
    location: {
      search: "?utm_source=newsletter",
      origin: "https://www.agentmiddleware.dev",
    },
    URLSearchParams,
    URL,
    sessionStorage: {
      getItem(key) { return session.has(key) ? session.get(key) : null; },
      setItem(key, value) { session.set(key, String(value)); },
    },
  });
  runInContext(captureScript, context, { filename: "utm-capture.js" });
  runInContext(script, context, { filename: "demo-key.js" });
  assert.equal(typeof listeners["get-demo-key"], "function");
  assert.equal(JSON.parse(session.get("amwDemoKeyUtm")).utm_source, "newsletter");
});
