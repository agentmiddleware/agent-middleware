import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { createContext, runInContext } from "node:vm";

const script = readFileSync(new URL("./try/try.js", import.meta.url), "utf8");
const captureScript = readFileSync(new URL("./utm-capture.js", import.meta.url), "utf8");
const html = readFileSync(new URL("./try/index.html", import.meta.url), "utf8");
const DEMO_KEY_URL = "https://api.thisisatest.tech/v1/demo/keys";
const SIGNUP_URL = "https://cqtlzicxmaklslvxderm.supabase.co/functions/v1/amw-test-signup";

function makePage(fetchResponse, options = {}) {
  const ids = [
    "get-try-key",
    "try-key-status",
    "try-key-result",
    "try-api-key",
    "try-wallet-id",
    "try-email",
    "try-email-error",
    "copy-try-key",
    "copy-try-wallet",
  ];
  const elements = new Map(ids.map((id) => [id, {
    disabled: false,
    hidden: id === "try-key-result" || id === "try-email-error",
    textContent: "",
    value: "",
    listeners: {},
    addEventListener(event, listener) { this.listeners[event] = listener; },
    focus() {},
    select() {},
    set innerHTML(_) { throw new Error("API data must not be inserted as HTML"); },
  }]));
  const requests = [];
  const copies = [];
  const session = new Map(Object.entries(options.session || {}));
  const forbidden = () => { throw new Error("key must not be persisted, logged, or sent to analytics"); };
  const context = createContext({
    document: {
      referrer: options.referrer || "",
      getElementById(id) { return elements.get(id); },
      set cookie(_) { forbidden(); },
    },
    location: {
      search: options.search || "",
      origin: options.origin || "https://www.agentmiddleware.dev",
    },
    URLSearchParams,
    URL,
    async fetch(url, request) {
      requests.push([url, request]);
      if (url === DEMO_KEY_URL) return fetchResponse();
      if (url === SIGNUP_URL) {
        return options.signupResponse ? options.signupResponse() : { status: 201 };
      }
      throw new Error(`unexpected fetch URL: ${url}`);
    },
    navigator: {
      clipboard: { async writeText(value) { copies.push(value); } },
      sendBeacon: forbidden,
    },
    localStorage: { setItem: forbidden, getItem: forbidden },
    sessionStorage: {
      getItem(key) { return session.has(key) ? session.get(key) : null; },
      setItem(key, value) {
        assert.equal(key, "amwDemoKeyUtm");
        session.set(key, String(value));
      },
    },
    console: { log: forbidden, info: forbidden, warn: forbidden, error: forbidden },
    window: { va: forbidden, gtag: forbidden },
  });
  if (options.loadCapture !== false) {
    runInContext(captureScript, context, { filename: "utm-capture.js" });
  }
  runInContext(script, context, { filename: "try.js" });
  return {
    elements,
    requests,
    copies,
    session,
    async click(id) {
      const element = elements.get(id);
      assert.equal(typeof element?.listeners.click, "function", `${id} has a click handler`);
      if (!element.disabled) await element.listeners.click();
    },
  };
}

function signupRequests(page) {
  return page.requests.filter(([url]) => url === SIGNUP_URL);
}

function signupPayload(page) {
  const requests = signupRequests(page);
  assert.equal(requests.length, 1);
  const [, request] = requests[0];
  assert.equal(request.method, "POST");
  assert.equal(request.credentials, "omit");
  assert.equal(request.headers["Content-Type"], "application/json");
  return JSON.parse(request.body);
}

function issued(key = "test_key", wallet = "wallet_test") {
  return {
    status: 201,
    async json() { return { api_key: key, wallet_id: wallet, budget_credits: 25 }; },
  };
}

function shownValue(element) {
  return element.value || element.textContent;
}

function pageTokens(markup) {
  const tags = [];
  let visibleMainText = "";
  let inMain = false;
  let hiddenTag = null;
  let cursor = 0;

  while (cursor < markup.length) {
    const opening = markup.indexOf("<", cursor);
    if (opening === -1) {
      if (inMain && !hiddenTag) visibleMainText += markup.slice(cursor);
      break;
    }
    if (inMain && !hiddenTag) visibleMainText += markup.slice(cursor, opening);

    if (markup.startsWith("<!--", opening)) {
      const closing = markup.indexOf("-->", opening + 4);
      assert.notEqual(closing, -1, "HTML comment is closed");
      cursor = closing + 3;
      continue;
    }

    let closing = opening + 1;
    let quote = null;
    for (; closing < markup.length; closing++) {
      const character = markup[closing];
      if (quote) {
        if (character === quote) quote = null;
      } else if (character === '"' || character === "'") {
        quote = character;
      } else if (character === ">") {
        break;
      }
    }
    assert.ok(closing < markup.length, "HTML tag is closed");
    const raw = markup.slice(opening + 1, closing).trim();
    const parsed = /^(\/?)\s*([a-z][\w:-]*)/i.exec(raw);
    if (parsed) {
      const tag = { name: parsed[2].toLowerCase(), closing: parsed[1] === "/", raw };
      tags.push(tag);
      if (hiddenTag) {
        if (tag.closing && tag.name === hiddenTag) hiddenTag = null;
      } else if (tag.name === "main") {
        inMain = !tag.closing;
      } else if (inMain && !tag.closing && (tag.name === "script" || tag.name === "style")) {
        hiddenTag = tag.name;
      }
    }
    cursor = closing + 1;
  }

  assert.ok(tags.some((tag) => tag.name === "main" && !tag.closing), "page has main content");
  return { tags, visibleMainText };
}

test("one demo POST shows key and wallet once, copies both, and sends one credential free signup", async () => {
  const key = "demo_<img src=x onerror=alert(1)>";
  const wallet = "wallet_<script>bad()</script>";
  const page = makePage(async () => issued(key, wallet));

  await page.click("get-try-key");
  assert.equal(page.requests.length, 2);
  const [url, request] = page.requests[0];
  assert.equal(url, DEMO_KEY_URL);
  assert.equal(request.method, "POST");
  assert.equal(request.credentials, "omit");
  assert.deepEqual(Object.keys(request).sort(), ["credentials", "method"]);
  assert.equal(page.elements.get("get-try-key").disabled, true);
  assert.equal(page.elements.get("try-key-result").hidden, false);
  assert.equal(shownValue(page.elements.get("try-api-key")), key);
  assert.equal(shownValue(page.elements.get("try-wallet-id")), wallet);
  const displayed = [...page.elements.values()].map((element) => `${element.value}${element.textContent}`).join(" ");
  assert.equal(displayed.split(key).length - 1, 1);
  assert.equal(displayed.split(wallet).length - 1, 1);
  const payload = signupPayload(page);
  assert.deepEqual(payload, { wallet_id: wallet });
  assert.equal(Object.hasOwn(payload, "api_key"), false);
  assert.equal(JSON.stringify(payload).includes(key), false);

  await page.click("copy-try-key");
  await page.click("copy-try-wallet");
  assert.deepEqual(page.copies, [key, wallet]);
  assert.doesNotMatch(page.elements.get("try-key-status").textContent, /[-\u2013\u2014]/);
  await page.click("get-try-key");
  assert.equal(page.requests.length, 2);
  assert.equal(signupRequests(page).length, 1);
  assert.equal(page.session.size, 0);
});

test("stored UTM is overridden by URL values and first external referrer is retained", async () => {
  const page = makePage(async () => issued(), {
    search: "?utm_source=newsletter&utm_medium=%20&utm_term=receipts",
    referrer: " https://second.example/item ",
    session: {
      amwDemoKeyUtm: JSON.stringify({
        utm_source: "old-source",
        utm_campaign: "launch",
        referrer: "https://first.example/landing",
      }),
    },
  });

  await page.click("get-try-key");
  const [, request] = page.requests[0];
  assert.equal(request.headers["Content-Type"], "application/json");
  assert.deepEqual(JSON.parse(request.body), {
    utm_source: "newsletter",
    utm_campaign: "launch",
    utm_term: "receipts",
    referrer: "https://first.example/landing",
  });
  assert.deepEqual(JSON.parse(page.session.get("amwDemoKeyUtm")), JSON.parse(request.body));
  assert.equal([...page.session.values()].some((value) => value.includes("test_key")), false);
  assert.deepEqual(signupPayload(page), {
    wallet_id: "wallet_test",
    utm_source: "newsletter",
    utm_campaign: "launch",
    utm_term: "receipts",
    referrer: "https://first.example/landing",
  });
});

test("valid optional email is trimmed and sent with wallet attribution", async () => {
  const page = makePage(async () => issued("secret_key", "wallet_email"), {
    search: "?utm_source=docs",
    referrer: "https://partner.example/guide",
  });
  page.elements.get("try-email").value = "  user@example.com  ";

  await page.click("get-try-key");
  assert.deepEqual(signupPayload(page), {
    wallet_id: "wallet_email",
    email: "user@example.com",
    utm_source: "docs",
    referrer: "https://partner.example/guide",
  });
  assert.equal(page.elements.get("try-email-error").textContent, "");
  assert.equal(page.elements.get("try-email-error").hidden, true);
});

test("invalid optional email is omitted while wallet signup and key display continue", async () => {
  for (const email of ["not-an-email", `${"a".repeat(250)}@x.co`]) {
    const page = makePage(async () => issued("secret_key", "wallet_invalid"));
    page.elements.get("try-email").value = email;

    await page.click("get-try-key");
    assert.deepEqual(signupPayload(page), { wallet_id: "wallet_invalid" });
    assert.notEqual(page.elements.get("try-email-error").textContent, "");
    assert.equal(page.elements.get("try-email-error").hidden, false);
    assert.equal(page.elements.get("try-key-result").hidden, false);
    assert.equal(shownValue(page.elements.get("try-api-key")), "secret_key");
  }
});

test("signup failure is silent and leaves the issued key visible", async () => {
  const page = makePage(async () => issued("secret_key", "wallet_failure"), {
    signupResponse: async () => { throw new Error("signup unavailable"); },
  });

  await page.click("get-try-key");
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(signupPayload(page), { wallet_id: "wallet_failure" });
  assert.equal(page.elements.get("try-key-result").hidden, false);
  assert.equal(shownValue(page.elements.get("try-api-key")), "secret_key");
  assert.equal(page.elements.get("try-key-status").textContent, "Test key ready. Copy it now.");
});

test("UTM and referrer are capped before storage and request", async () => {
  const page = makePage(async () => issued(), {
    search: `?utm_source=${encodeURIComponent(`  ${"s".repeat(140)}  `)}&utm_campaign=${"c".repeat(200)}&utm_medium=%20`,
    referrer: ` https://news.example/${"r".repeat(600)} `,
  });
  const stored = JSON.parse(page.session.get("amwDemoKeyUtm"));
  assert.equal(stored.utm_source, "s".repeat(128));
  assert.equal(stored.utm_campaign, "c".repeat(128));
  assert.equal("utm_medium" in stored, false);
  assert.equal(stored.referrer.length, 512);

  await page.click("get-try-key");
  assert.deepEqual(JSON.parse(page.requests[0][1].body), stored);
});

test("same origin referrer is sent only for this visit and standalone try.js still attributes", async () => {
  const page = makePage(async () => issued(), {
    referrer: " https://www.agentmiddleware.dev/ ",
  });
  assert.equal(page.session.size, 0);
  await page.click("get-try-key");
  assert.deepEqual(JSON.parse(page.requests[0][1].body), {
    referrer: "https://www.agentmiddleware.dev/",
  });
  assert.equal(page.session.size, 0);

  const direct = makePage(async () => issued(), {
    search: "?utm_source=direct",
    loadCapture: false,
  });
  await direct.click("get-try-key");
  assert.deepEqual(JSON.parse(direct.requests[0][1].body), { utm_source: "direct" });
});

test("failed requests leave the key hidden and allow retry", async () => {
  for (const status of [403, 404, 429, 503]) {
    let count = 0;
    const page = makePage(async () => ++count === 1 ? { status } : issued());
    await page.click("get-try-key");
    assert.equal(page.elements.get("get-try-key").disabled, false);
    assert.equal(page.elements.get("try-key-result").hidden, true);
    assert.ok(page.elements.get("try-key-status").textContent);
    assert.doesNotMatch(page.elements.get("try-key-status").textContent, /[-\u2013\u2014]/);
    await page.click("get-try-key");
    assert.equal(page.requests.filter(([url]) => url === DEMO_KEY_URL).length, 2);
    assert.equal(signupRequests(page).length, 1);
    assert.equal(page.elements.get("try-key-result").hidden, false);
  }

  const network = makePage(async () => { throw new Error("offline"); });
  await network.click("get-try-key");
  assert.equal(network.elements.get("get-try-key").disabled, false);
  assert.equal(network.elements.get("try-key-result").hidden, true);

  const unreadable = makePage(async () => ({ status: 201, async json() { throw new Error("bad JSON"); } }));
  await unreadable.click("get-try-key");
  assert.equal(unreadable.elements.get("get-try-key").disabled, false);
  assert.equal(unreadable.elements.get("try-key-result").hidden, true);

  const invalid = makePage(async () => ({ status: 201, async json() { return { api_key: "key" }; } }));
  await invalid.click("get-try-key");
  assert.equal(invalid.elements.get("get-try-key").disabled, false);
  assert.equal(invalid.elements.get("try-key-result").hidden, true);
});

test("page uses external assets and visible copy contains no dashes", () => {
  const { tags, visibleMainText } = pageTokens(html);
  const openingTags = tags.filter((tag) => !tag.closing);
  assert.ok(openingTags.some((tag) => tag.name === "script" && tag.raw.includes('src="/utm-capture.js?v=')));
  assert.ok(openingTags.some((tag) => tag.name === "script" && tag.raw.includes('src="/try/try.js?v=')));
  assert.ok(html.indexOf("/utm-capture.js") < html.indexOf("/try/try.js"));
  assert.ok(openingTags.some((tag) => tag.name === "link" && tag.raw.includes('href="/try/try.css?v=')));
  for (const tag of openingTags) {
    assert.notEqual(tag.name, "style", "page has no inline stylesheet");
    assert.doesNotMatch(tag.raw, /\sstyle\s*=/i);
    if (tag.name === "script") {
      assert.ok(
        tag.raw.includes(' src="') || tag.raw.includes(' type="application/ld+json"'),
        "page has no executable inline script",
      );
    }
  }
  assert.ok(openingTags.some((tag) => tag.name === "button" && tag.raw.includes('id="get-try-key"')));
  assert.ok(openingTags.some((tag) => tag.name === "label" && tag.raw.includes('for="try-email"')));
  assert.ok(openingTags.some((tag) => tag.name === "input" && tag.raw.includes('id="try-email"') && tag.raw.includes('type="email"') && tag.raw.includes('autocomplete="email"')));
  assert.ok(visibleMainText.includes("Email (optional)"));
  assert.ok(visibleMainText.includes("Leave it blank"));
  assert.ok(visibleMainText.includes("Get a free test key"));
  for (const id of ["copy-try-key", "copy-try-wallet"]) {
    assert.ok(openingTags.some((tag) => tag.name === "button" && tag.raw.includes(`id="${id}"`)));
  }
  assert.ok(openingTags.some((tag) => tag.name === "a" && tag.raw.includes('href="https://github.com/agentmiddleware/amw-demo')));
  assert.doesNotMatch(visibleMainText, /[-\u2013\u2014]|&(?:ndash|mdash|hyphen|#0*45|#x0*2d);/i);
  assert.doesNotMatch(visibleMainText, /no second call or charge/i);
});

test("visible copy scanner ignores markup, comments, and hidden element contents", () => {
  const sample = '<main data-note="1 > 0"><!-- hidden - --><script>hidden -</script><style>hidden -</style><p>Visible &mdash;</p></main>';
  const { visibleMainText } = pageTokens(sample);
  assert.equal(visibleMainText, "Visible &mdash;");
});
