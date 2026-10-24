import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const css = readFileSync(new URL("./styles.css", import.meta.url), "utf8");

function rule(selector) {
  const start = css.indexOf(`${selector} {`);
  assert.notEqual(start, -1, `Missing ${selector}`);
  const body = css.slice(start + selector.length + 2, css.indexOf("}", start));
  return Object.fromEntries([...body.matchAll(/([\w-]+):\s*([^;]+);/g)].map((match) => [match[1], match[2].trim()]));
}

function rgb(hex) {
  assert.match(hex, /^#[0-9a-f]{6}$/i);
  return [1, 3, 5].map((offset) => parseInt(hex.slice(offset, offset + 2), 16));
}

function contrast(foreground, background) {
  const luminance = (color) => {
    const channels = color.map((value) => {
      const channel = value / 255;
      return channel <= 0.04045 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4;
    });
    return channels[0] * 0.2126 + channels[1] * 0.7152 + channels[2] * 0.0722;
  };
  const [lighter, darker] = [luminance(foreground), luminance(background)].sort((a, b) => b - a);
  return (lighter + 0.05) / (darker + 0.05);
}

test("homepage high contrast palette has readable text, accents, and boundaries", () => {
  const root = rule('html[data-a11y-contrast="high"]');
  const home = rule('html[data-a11y-contrast="high"] body.site-light');
  const color = (token) => rgb(root[home[token].slice(4, -1)]);
  const background = color("--home-bg");
  const panel = color("--home-soft");
  assert.ok(contrast(color("--home-text"), background) >= 7);
  assert.ok(contrast(color("--home-muted"), panel) >= 7);
  assert.ok(contrast(color("--home-primary-text"), color("--home-blue")) >= 7);
  assert.ok(contrast(color("--home-error"), background) >= 4.5);

  assert.equal(home["--home-line"], "var(--line-strong)");
  const [, red, green, blue, alpha] = root["--line-strong"].match(/^rgba\((\d+), (\d+), (\d+), ([\d.]+)\)$/);
  const line = [red, green, blue].map((channel, index) =>
    Number(channel) * Number(alpha) + panel[index] * (1 - Number(alpha)));
  assert.ok(contrast(line, panel) >= 3);
});

test("homepage surfaces and controls use the contrast aware palette", () => {
  for (const [selector, property, value] of [
    ["body.site-light #main", "background", "var(--home-bg)"],
    ["body.site-light .site-nav", "background", "var(--home-bg)"],
    ["body.site-light .site-nav", "border-bottom", "1px solid var(--home-line)"],
    ["body.site-light .nav-links a", "color", "var(--home-muted)"],
    ["body.site-light .nav-links .nav-agent", "border", "1px solid var(--home-line)"],
    ["body.home-light .hero-v2-action-primary,\nbody.home-light #main .hero-v2-action-primary", "color", "var(--home-primary-text)"],
    ["body.home-light .hero-v2-code", "background", "var(--home-soft)"],
    ["body.home-light .hero-v2-command-row button", "border", "1px solid var(--home-line)"],
    ["body.home-light .hero-v2-tab-disabled span", "border", "1px solid #6b7280"],
    ["body.home-light .hero-v2-step", "background", "var(--home-bg)"],
    ["body.home-light .hero-v2-intro > p", "color", "var(--home-muted)"],
    ["body.home-light .hero-v2-intro > .hero-v2-boundary", "font-size", "0.875rem"],
    ["body.home-light .home-key-drawer", "background", "var(--home-bg)"],
    ["body.home-light .home-key-field-row", "border", "1px solid var(--home-line)"],
    ["body.home-light .home-key-field-row button", "color", "var(--home-muted)"],
    ["body.home-light .home-v2-glossary", "background", "var(--home-soft)"],
  ]) {
    assert.equal(rule(selector)[property], value, `${selector} ${property}`);
  }
  assert.equal(rule('html[data-a11y-contrast="high"] body.home-light .home-key-drawer')["border-left"], "1px solid var(--home-line)");
  assert.equal(rule('html[data-a11y-contrast="high"] body.home-light .home-key-field-row button')["border-left"], "1px solid var(--home-line)");
});

test("site light text, controls, and status colors meet AA on light surfaces", () => {
  const site = rule("body.site-light");
  const color = (token) => rgb(site[token]);
  const background = color("--home-bg");
  const panel = color("--home-soft");
  assert.ok(contrast(color("--home-text"), background) >= 7);
  assert.ok(contrast(color("--home-muted"), background) >= 4.5);
  assert.ok(contrast(color("--home-muted"), panel) >= 4.5);
  assert.ok(contrast(color("--home-blue"), background) >= 4.5);
  assert.ok(contrast(color("--home-primary-text"), color("--home-blue")) >= 4.5);
  assert.ok(contrast(color("--home-blue-hover"), background) >= 4.5);
  assert.ok(contrast(color("--home-control-line"), background) >= 3);
  assert.ok(contrast(color("--home-verify"), panel) >= 4.5);
});
