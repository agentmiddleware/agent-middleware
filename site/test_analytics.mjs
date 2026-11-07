import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { createContext, runInContext } from "node:vm";

const script = readFileSync(new URL("./analytics.js", import.meta.url), "utf8");
const homeHtml = readFileSync(new URL("./index.html", import.meta.url), "utf8");

function analyticsPage(analyticsEnabled) {
  const sent = [];
  let click;
  const context = createContext({
    document: {
      addEventListener(name, listener) {
        assert.equal(name, "click");
        click = listener;
      },
    },
    window: analyticsEnabled ? {
      va(...args) { sent.push(JSON.parse(JSON.stringify(args))); },
    } : {},
  });
  runInContext(script, context, { filename: "analytics.js" });
  return {
    sent,
    click(eventName) {
      click({
        target: {
          closest(selector) {
            assert.equal(selector, "[data-analytics-event]");
            return eventName ? { dataset: { analyticsEvent: eventName } } : null;
          },
        },
      });
    },
  };
}

test("homepage demo key button emits one existing analytics event per click", () => {
  assert.match(homeHtml, /<button[^>]*id="home-key-trigger"[^>]*data-analytics-event="demo_key_click"[^>]*>/);
  const page = analyticsPage(true);
  page.click("demo_key_click");
  assert.deepEqual(page.sent, [["event", { name: "demo_key_click" }]]);
});

test("analytics stays silent without opt in and rejects unknown event names", () => {
  const disabled = analyticsPage(false);
  disabled.click("demo_key_click");
  assert.deepEqual(disabled.sent, []);

  const enabled = analyticsPage(true);
  enabled.click("receipt_verified");
  enabled.click("");
  assert.deepEqual(enabled.sent, []);
});
