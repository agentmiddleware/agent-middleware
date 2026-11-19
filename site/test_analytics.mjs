import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { readFileSync } from "node:fs";
import test from "node:test";
import { createContext, runInContext } from "node:vm";

const script = readFileSync(new URL("./analytics.js", import.meta.url), "utf8");
const clientScript = readFileSync(new URL("./posthog.js", import.meta.url), "utf8");
const utmScript = readFileSync(new URL("./utm-capture.js", import.meta.url), "utf8");
const homeHtml = readFileSync(new URL("./index.html", import.meta.url), "utf8");

function analyticsPage(options = {}) {
  const key = options.key ?? randomUUID();
  const sent = [];
  const loaded = [];
  const vaCalls = [];
  const session = new Map(Object.entries(options.session || {}));
  let click;
  let domReady;
  const window = {
    crypto: { randomUUID },
    va(...args) { vaCalls.push(args); },
  };
  const context = createContext({
    document: {
      readyState: options.readyState || "interactive",
      referrer: options.referrer || "",
      querySelector(selector) {
        const values = {
          'meta[name="amw-posthog-key"]': key,
          'meta[name="amw-posthog-host"]': options.host ?? "https://us.i.posthog.com",
        };
        return values[selector] ? { content: values[selector] } : null;
      },
      addEventListener(name, listener, options) {
        if (name === "DOMContentLoaded") {
          assert.equal(options.once, true);
          domReady = listener;
        } else {
          assert.equal(name, "click");
          click = listener;
        }
      },
      createElement(tag) {
        assert.equal(tag, "script");
        return {};
      },
      head: { appendChild(element) { loaded.push(element); } },
    },
    location: {
      pathname: options.pathname || "/proof/",
      search: options.search || "?email=private%40example.org",
      href: "https://www.agentmiddleware.dev/proof/?email=private%40example.org#private",
      origin: "https://www.agentmiddleware.dev",
    },
    sessionStorage: {
      getItem(key) { return session.get(key) || null; },
      setItem(key, value) { session.set(key, String(value)); },
    },
    fetch(url, request) {
      if (options.fetchThrows) throw new Error("blocked");
      sent.push({ url, request: JSON.parse(JSON.stringify(request)), body: JSON.parse(request.body) });
      return options.fetchRejects ? Promise.reject(new Error("offline")) : Promise.resolve({ ok: true });
    },
    URL,
    URLSearchParams,
    window,
  });
  runInContext(script, context, { filename: "analytics.js" });
  return {
    key, sent, loaded, vaCalls, session, window,
    ready() {
      context.document.readyState = "complete";
      const listener = domReady;
      domReady = undefined;
      listener?.();
    },
    init() { runInContext(script, context, { filename: "analytics.js" }); },
    load() { runInContext(clientScript, context, { filename: "posthog.js" }); },
    utms() { runInContext(utmScript, context, { filename: "utm-capture.js" }); },
    dispatch(event) { click?.(event); },
    click(eventName, ctaId = "home-key-trigger") {
      click?.({
        target: {
          closest(selector) {
            assert.equal(selector, "[data-analytics-event]");
            return eventName ? {
              dataset: { analyticsEvent: eventName, ctaId },
              href: "mailto:private@example.org?body=private",
              textContent: "private@example.org",
              value: "form-content",
            } : null;
          },
        },
      });
    },
  };
}

test("homepage demo key button emits one event with the exact allowed properties", () => {
  assert.match(homeHtml, /<button[^>]*id="home-key-trigger"[^>]*data-analytics-event="demo_key_click"[^>]*data-cta-id="home-key-trigger"[^>]*>/);
  const page = analyticsPage({ pathname: "/" });
  assert.equal(page.loaded.length, 1);
  assert.equal(page.loaded[0].src, "/posthog.js?v=posthog-1");
  assert.equal(page.loaded[0].async, true);
  page.load();
  page.ready();
  assert.equal(page.sent.length, 1);
  assert.equal(page.sent[0].body.event, "$pageview");
  page.click("demo_key_click");
  assert.equal(page.sent.length, 2);
  const { url, request, body } = page.sent[1];
  assert.equal(url, "https://us.i.posthog.com/i/v0/e/");
  assert.deepEqual(Object.keys(body).sort(), ["api_key", "distinct_id", "event", "properties"]);
  assert.equal(body.api_key, page.key);
  assert.match(body.distinct_id, /^[\da-f-]{36}$/);
  assert.equal(body.event, "demo_key_click");
  assert.deepEqual(body.properties, { product: "AMW", page: "/", cta_id: "home-key-trigger", $process_person_profile: false });
  assert.equal(request.method, "POST");
  assert.equal(request.keepalive, true);
  assert.equal(request.credentials, "omit");
  assert.equal(request.referrerPolicy, "no-referrer");
  assert.equal(request.headers["Content-Type"], "application/json");
  assert.deepEqual(page.vaCalls, []);
});

test("the existing event names are preserved, with no request_access alias", () => {
  const page = analyticsPage({ host: "https://eu.i.posthog.com" });
  page.load();
  page.ready();
  for (const event of ["email_click", "demo_key_click", "proof_click", "booking_click"]) page.click(event);
  page.click("request_access");
  page.click("receipt_verified");
  page.click("");
  assert.deepEqual(page.sent.map(({ body }) => body.event), ["$pageview", "email_click", "demo_key_click", "proof_click", "booking_click"]);
  assert.ok(page.sent.every(({ url }) => url === "https://eu.i.posthog.com/i/v0/e/"));
  assert.ok(page.sent.every(({ body }) => body.properties.$process_person_profile === false));
  assert.equal(new Set(page.sent.map(({ body }) => body.distinct_id)).size, 1);
  assert.deepEqual(page.vaCalls, []);
});

test("no configured key means no client, listener, requests, or Vercel calls", () => {
  for (const key of ["", "  "]) {
    const page = analyticsPage({ key, search: "?utm_source=newsletter" });
    page.utms();
    page.ready();
    page.click("demo_key_click");
    page.load(); // Even loading the file directly cannot enable it.
    assert.equal(page.window.amwPosthog, undefined);
    assert.deepEqual(page.loaded, []);
    assert.deepEqual(page.sent, []);
    assert.deepEqual(page.vaCalls, []);
    assert.deepEqual(JSON.parse(page.session.get("amwDemoKeyUtm")), { utm_source: "newsletter" });
  }
});

test("missing or unsafe hosts fail closed without loading anything", () => {
  for (const host of ["", "http://us.i.posthog.com", "https://us.i.posthog.com/?email=private", "https://us.i.posthog.com.example.org"]) {
    const page = analyticsPage({ host });
    page.ready();
    page.click("email_click");
    assert.deepEqual(page.loaded, []);
    assert.deepEqual(page.sent, []);
  }
});

test("queued pageview, attribution and clicks survive delayed loading without duplicate events", () => {
  const page = analyticsPage({
    search: "?utm_source=newsletter&utm_term=" + "t".repeat(200),
    referrer: "https://news.example/article?email=private@example.org",
    session: { amwDemoKeyUtm: JSON.stringify({ utm_campaign: "launch", email: "private@example.org" }) },
  });
  page.utms();
  page.ready();
  page.click("proof_click", "proof-download");
  assert.equal(page.sent.length, 0);
  page.init();
  assert.equal(page.loaded.length, 1);
  page.load();
  assert.equal(page.sent.length, 2);
  assert.deepEqual(page.sent.map(({ body }) => body.event), ["$pageview", "proof_click"]);
  assert.deepEqual(page.sent[0].body.properties, {
    utm_source: "newsletter", utm_campaign: "launch", utm_term: "t".repeat(128),
    product: "AMW", page: "/proof/", $process_person_profile: false,
  });
  assert.deepEqual(page.sent[1].body.properties, {
    utm_source: "newsletter", utm_campaign: "launch", utm_term: "t".repeat(128),
    product: "AMW", page: "/proof/", cta_id: "proof-download", $process_person_profile: false,
  });
  assert.equal(JSON.parse(page.session.get("amwDemoKeyUtm")).referrer, "https://news.example/article?email=private@example.org");
  page.load();
  page.ready();
  assert.equal(page.sent.length, 2);
  page.click("email_click", "footer-contact");
  assert.equal(page.sent.length, 3);
});

test("pageview waits for attribution when the client loads before UTM capture", () => {
  const page = analyticsPage({ search: "?utm_medium=email&utm_content=footer" });
  page.load();
  assert.deepEqual(page.sent, []);
  page.utms();
  page.init();
  page.ready();
  page.ready();
  page.click("email_click", "footer-contact");
  assert.deepEqual(page.sent.map(({ body }) => body.event), ["$pageview", "email_click"]);
  assert.deepEqual(page.sent[0].body.properties, {
    utm_medium: "email", utm_content: "footer", product: "AMW", page: "/proof/", $process_person_profile: false,
  });
  assert.deepEqual(page.sent[1].body.properties, {
    utm_medium: "email", utm_content: "footer", product: "AMW", page: "/proof/", cta_id: "footer-contact", $process_person_profile: false,
  });
});

test("pageview captures once when analytics initializes after DOM readiness", () => {
  const page = analyticsPage({ readyState: "complete" });
  page.init();
  page.load();
  page.ready();
  page.init();
  page.load();
  assert.equal(page.sent.length, 1);
  assert.equal(page.sent[0].body.event, "$pageview");
  assert.deepEqual(page.sent[0].body.properties, { product: "AMW", page: "/proof/", $process_person_profile: false });
  assert.equal(page.sent[0].request.referrerPolicy, "no-referrer");
});

test("transport boundary strips extra properties and rejects unapproved events", () => {
  const page = analyticsPage();
  page.load();
  const privateFields = {
    email: "private@example.org", name: "private", referrer: "https://news.example/?private",
    $current_url: "https://www.agentmiddleware.dev/?private", $set: { email: "private@example.org" },
    $process_person_profile: true,
    utm_unknown: "ignored", utm_campaign: { form: "private" },
  };
  page.window.amwPosthog.register({ ...privateFields, utm_source: "  newsletter  ", utm_term: "t".repeat(200) });
  page.window.amwPosthog.capture("email_click", {
    ...privateFields, product: "overridden", page: "/pilot/?private#private", cta_id: "pilot-contact",
  });
  assert.deepEqual(page.sent[0].body.properties, {
    utm_source: "newsletter", utm_term: "t".repeat(128), product: "AMW", page: "/pilot/", cta_id: "pilot-contact", $process_person_profile: false,
  });
  page.window.amwPosthog.capture("$pageview", {
    ...privateFields, product: "overridden", page: "/pilot/?private#private", cta_id: "pilot-contact",
    utm_source: "unregistered", utm_medium: "unregistered",
  });
  assert.equal(page.sent[1].body.event, "$pageview");
  assert.deepEqual(page.sent[1].body.properties, {
    utm_source: "newsletter", utm_term: "t".repeat(128), product: "AMW", page: "/pilot/", $process_person_profile: false,
  });
  for (const event of ["request_access", "$autocapture", "$identify", "$snapshot"]) {
    page.window.amwPosthog.capture(event, { page: "/", cta_id: "nav-contact" });
  }
  for (const path of ["https://www.agentmiddleware.dev/?private", "//news.example/", "", undefined]) {
    page.window.amwPosthog.capture("email_click", { page: path, cta_id: "nav-contact" });
    page.window.amwPosthog.capture("$pageview", { page: path });
  }
  assert.doesNotThrow(() => page.window.amwPosthog.capture("$pageview"));
  assert.equal(page.sent.length, 2);
});

test("clicks without approved names and explicit safe CTA ids are ignored", () => {
  const page = analyticsPage();
  page.load();
  page.ready();
  page.dispatch({ target: null });
  page.dispatch({ target: {} });
  for (const id of ["", "private@example.org", "https://example.org/?private", "a".repeat(81)]) page.click("email_click", id);
  assert.deepEqual(page.sent.map(({ body }) => body.event), ["$pageview"]);
});

test("blocked client loading and analytics errors do not interfere with CTA clicks", async () => {
  const blocked = analyticsPage();
  for (let i = 0; i < 60; i++) blocked.click("demo_key_click");
  assert.equal(blocked.window.amwPosthog.queue.length, 50);
  blocked.loaded[0].onerror();
  assert.doesNotThrow(() => blocked.ready());
  assert.doesNotThrow(() => blocked.click("demo_key_click"));
  assert.deepEqual(blocked.sent, []);
  for (const options of [{ fetchThrows: true }, { fetchRejects: true }]) {
    const page = analyticsPage(options);
    page.load();
    assert.doesNotThrow(() => page.ready());
    assert.doesNotThrow(() => page.click("email_click"));
    await new Promise(resolve => setImmediate(resolve));
  }
  const page = analyticsPage();
  page.window.amwPosthog.capture = () => { throw new Error("unavailable"); };
  assert.doesNotThrow(() => page.click("email_click"));
});

test("identity is generated per page without browser storage", () => {
  const first = analyticsPage();
  const second = analyticsPage();
  for (const page of [first, second]) {
    page.load();
    page.ready();
    page.click("proof_click");
    assert.equal(page.session.size, 0);
  }
  assert.notEqual(first.sent[0].body.distinct_id, second.sent[0].body.distinct_id);
});
