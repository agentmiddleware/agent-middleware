import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { runInNewContext } from "node:vm";

const script = readFileSync(new URL("./arcade-boot.js", import.meta.url), "utf8");

function launch(scriptSrc, styleHref) {
  const listeners = [];
  const launcher = {
    hidden: true,
    getAttribute(name) {
      return name === "data-arcade-script" ? scriptSrc : styleHref;
    },
    closest() { return null; },
    addEventListener(name) { listeners.push(name); },
  };
  const document = {
    getElementById() { return launcher; },
    createElement(name) {
      return name === "canvas" ? { getContext() { return {}; } } : {};
    },
  };
  const location = { href: "https://www.agentmiddleware.dev/", origin: "https://www.agentmiddleware.dev", search: "" };
  runInNewContext(script, { document, location, window: { location }, URL, URLSearchParams });
  return { launcher, listeners };
}

test("same-origin JavaScript and CSS assets reveal the launcher", () => {
  const { launcher, listeners } = launch("/arcade.js", "/arcade.css");
  assert.equal(launcher.hidden, false);
  assert.deepEqual(listeners, ["click", "pointerenter", "focus"]);
});

test("cross-origin asset is rejected", () => {
  const { launcher, listeners } = launch("https://api.thisisatest.tech/arcade.js", "/arcade.css");
  assert.equal(launcher.hidden, true);
  assert.deepEqual(listeners, []);
});

test("non-HTTP asset is rejected", () => {
  const { launcher, listeners } = launch("file:///arcade.js", "/arcade.css");
  assert.equal(launcher.hidden, true);
  assert.deepEqual(listeners, []);
});

test("assets must have the expected extensions", () => {
  for (const [scriptSrc, styleHref] of [
    ["/arcade.css", "/arcade.css"],
    ["/arcade.js", "/arcade.js"],
  ]) {
    const { launcher, listeners } = launch(scriptSrc, styleHref);
    assert.equal(launcher.hidden, true);
    assert.deepEqual(listeners, []);
  }
});
