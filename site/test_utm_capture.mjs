import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { runInNewContext } from "node:vm";

const script = readFileSync(new URL("./utm-capture.js", import.meta.url), "utf8");

function capture(options = {}) {
  const session = new Map(Object.entries(options.session || {}));
  const registered = [];
  let writes = 0;
  runInNewContext(script, {
    document: { referrer: options.referrer || "" },
    location: {
      search: options.search || "",
      origin: options.origin || "https://www.agentmiddleware.dev",
    },
    URLSearchParams,
    URL,
    window: options.noAnalytics ? {} : {
      amwPosthog: {
        register(properties) {
          if (options.analyticsThrows) throw new Error("blocked");
          registered.push(JSON.parse(JSON.stringify(properties)));
        },
      },
    },
    sessionStorage: {
      getItem(key) {
        if (options.storageThrows) throw new Error("blocked");
        return session.has(key) ? session.get(key) : null;
      },
      setItem(key, value) {
        if (options.storageThrows) throw new Error("blocked");
        writes += 1;
        session.set(key, String(value));
      },
    },
  }, { filename: "utm-capture.js" });
  return { session, writes, registered };
}

test("landing UTM is trimmed, capped, and merged into the demo-key record", () => {
  const { session } = capture({
    search: "?utm_source=%20newsletter%20&utm_medium=%20&utm_term=" + "t".repeat(200),
    referrer: "https://news.example/item",
    session: {
      amwDemoKeyUtm: JSON.stringify({
        utm_source: "old-source",
        utm_campaign: "  launch  ",
        utm_content: "   ",
        ignored: "nope",
      }),
    },
  });

  assert.deepEqual(JSON.parse(session.get("amwDemoKeyUtm")), {
    utm_source: "newsletter",
    utm_campaign: "launch",
    utm_term: "t".repeat(128),
    referrer: "https://news.example/item",
  });
  assert.equal(session.get("amwDemoKeyUtm").includes("t".repeat(129)), false);
});

test("the first external referrer is stored once and same-origin referrers are not", () => {
  const first = capture({
    search: "?utm_source=newsletter",
    referrer: ` https://first.example/${"r".repeat(600)} `,
  });
  const storedReferrer = `https://first.example/${"r".repeat(600)}`.slice(0, 512);
  assert.equal(JSON.parse(first.session.get("amwDemoKeyUtm")).referrer, storedReferrer);
  assert.equal(JSON.parse(first.session.get("amwDemoKeyUtm")).referrer.length, 512);

  const later = capture({
    referrer: "https://second.example/later",
    session: { amwDemoKeyUtm: first.session.get("amwDemoKeyUtm") },
  });
  assert.equal(JSON.parse(later.session.get("amwDemoKeyUtm")).referrer, storedReferrer);

  const internal = capture({
    search: "?utm_campaign=home",
    referrer: "https://www.agentmiddleware.dev/compare/",
  });
  assert.deepEqual(JSON.parse(internal.session.get("amwDemoKeyUtm")), {
    utm_campaign: "home",
  });

  const bare = capture({ referrer: "https://www.agentmiddleware.dev/" });
  assert.equal(bare.session.size, 0);
  assert.equal(bare.writes, 0);
});

test("an already stored overlong value is capped on the next page", () => {
  const { session } = capture({
    session: {
      amwDemoKeyUtm: JSON.stringify({
        utm_source: "s".repeat(200),
        referrer: "https://news.example/" + "r".repeat(600),
      }),
    },
  });
  const stored = JSON.parse(session.get("amwDemoKeyUtm"));
  assert.equal(stored.utm_source, "s".repeat(128));
  assert.equal(stored.referrer.length, 512);
});

test("registers only merged, capped UTMs while retaining demo-key referrer storage", () => {
  const { session, registered } = capture({
    search: "?utm_source=%20newsletter%20&utm_term=" + "t".repeat(200) + "&email=private@example.org",
    referrer: "https://news.example/?private=value",
    session: { amwDemoKeyUtm: JSON.stringify({
      utm_source: "previous", utm_medium: " email ", utm_campaign: "c".repeat(200),
      utm_content: "  ", utm_other: "ignore", email: "private@example.org",
    }) },
  });
  const expected = {
    utm_source: "newsletter", utm_medium: "email", utm_campaign: "c".repeat(128), utm_term: "t".repeat(128),
  };
  assert.deepEqual(registered, [expected]);
  assert.deepEqual(JSON.parse(session.get("amwDemoKeyUtm")), { ...expected, referrer: "https://news.example/?private=value" });
});

test("internal page visits register attribution carried in sessionStorage", () => {
  const { registered } = capture({
    referrer: "https://www.agentmiddleware.dev/?private=value",
    session: { amwDemoKeyUtm: JSON.stringify({ utm_source: "newsletter", utm_content: "footer" }) },
  });
  assert.deepEqual(registered, [{ utm_source: "newsletter", utm_content: "footer" }]);
});

test("no UTMs means no registration, even if an external referrer is stored", () => {
  const { session, registered } = capture({ referrer: "https://news.example/?private=value" });
  assert.deepEqual(registered, []);
  assert.deepEqual(JSON.parse(session.get("amwDemoKeyUtm")), { referrer: "https://news.example/?private=value" });
});

test("missing or throwing analytics preserves demo-key storage", () => {
  for (const options of [{ noAnalytics: true }, { analyticsThrows: true }]) {
    const { session } = capture({ ...options, search: "?utm_source=newsletter" });
    assert.deepEqual(JSON.parse(session.get("amwDemoKeyUtm")), { utm_source: "newsletter" });
  }
});

test("blocked or malformed storage does not prevent registering URL attribution", () => {
  for (const options of [{ storageThrows: true }, { session: { amwDemoKeyUtm: "{" } }]) {
    const { registered } = capture({ ...options, search: "?utm_campaign=launch" });
    assert.deepEqual(registered, [{ utm_campaign: "launch" }]);
  }
});
