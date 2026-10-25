import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { runInNewContext } from "node:vm";

const script = readFileSync(new URL("./utm-capture.js", import.meta.url), "utf8");

function capture(options = {}) {
  const session = new Map(Object.entries(options.session || {}));
  let writes = 0;
  runInNewContext(script, {
    document: { referrer: options.referrer || "" },
    location: {
      search: options.search || "",
      origin: options.origin || "https://www.agentmiddleware.dev",
    },
    URLSearchParams,
    URL,
    sessionStorage: {
      getItem(key) { return session.has(key) ? session.get(key) : null; },
      setItem(key, value) {
        writes += 1;
        session.set(key, String(value));
      },
    },
  }, { filename: "utm-capture.js" });
  return { session, writes };
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
