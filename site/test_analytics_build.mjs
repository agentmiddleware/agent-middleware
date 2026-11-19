import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { randomBytes } from "node:crypto";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync } from "node:fs";
import { dirname, join } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const siteRoot = dirname(fileURLToPath(import.meta.url));
const repoRoot = dirname(siteRoot);
const sourcePages = [
  "index.html", "404.html", "pilot/index.html", "proof/index.html",
  "compare/index.html", "concept/index.html", "demo-key/index.html", "try/index.html",
];
const host = "https://us.i.posthog.com";
const freshKey = () => `phc_testkeyonly${randomBytes(24).toString("hex")}`;

function python(code, input) {
  return spawnSync("python3", ["-c", `
import json
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import build_site
payload = json.load(sys.stdin)
${code}
`, siteRoot], {
    cwd: repoRoot,
    input: JSON.stringify(input),
    encoding: "utf8",
    env: { ...process.env, PYTHONDONTWRITEBYTECODE: "1" },
  });
}

function configuration(environment) {
  const result = python(`
try:
    print(json.dumps({"markup": build_site.render_posthog_config(payload)}))
except build_site.LaunchConfigurationError as exc:
    print(json.dumps({"error": str(exc)}))
`, environment);
  assert.equal(result.status, 0, result.stderr);
  return JSON.parse(result.stdout);
}

function htmlFiles(directory) {
  return readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const path = join(directory, entry.name);
    return entry.isDirectory() ? htmlFiles(path) : entry.name.endsWith(".html") ? [path] : [];
  });
}

function build(environment, inspect, expectedStatus = 0) {
  mkdirSync(join(siteRoot, "dist"), { recursive: true });
  const temporary = mkdtempSync(join(siteRoot, "dist", "analytics-check-"));
  const output = join(temporary, "output");
  try {
    const result = python(`
# Isolate the real CLI build under site/dist without relaxing the production
# output-path guard or replacing an existing developer build.
build_site.DEFAULT_OUTPUT = Path(payload["output"])
import os
os.environ.clear()
os.environ.update(payload["environment"])
raise SystemExit(build_site.main(["--output", payload["output"]]))
`, {
      output,
      environment: {
        PUBLIC_DISPLAY_NAME: "AMW build validation",
        PUBLIC_CONTACT_EMAIL: "build-validation@agentmiddleware.dev",
        ...environment,
      },
    });
    assert.equal(result.status, expectedStatus, result.stderr);
    inspect(output, result);
  } finally {
    rmSync(temporary, { recursive: true, force: true });
  }
}

test("PostHog config stays absent without a key and ignores the legacy Vercel flag", () => {
  for (const environment of [
    {},
    { AMW_POSTHOG_KEY: " \n ", AMW_POSTHOG_HOST: "invalid" },
    { PUBLIC_ENABLE_VERCEL_ANALYTICS: "true" },
  ]) {
    assert.deepEqual(configuration(environment), { markup: "" });
  }
});

test("PostHog config defaults to the pinned US host and accepts its trailing slash", () => {
  const key = freshKey();
  for (const environment of [{}, { AMW_POSTHOG_HOST: host }, { AMW_POSTHOG_HOST: `${host}/` }]) {
    assert.deepEqual(configuration({ AMW_POSTHOG_KEY: ` ${key} `, ...environment }), {
      markup: `<meta name="amw-posthog-key" content="${key}" />\n    <meta name="amw-posthog-host" content="${host}" />`,
    });
  }
});

test("PostHog config rejects malformed capture keys without emitting or echoing them", () => {
  for (const key of ["phx_testkeyonly", "arbitrary string", "phc_", "phc_test_key", "phc_test-key", `${freshKey()}\"<&'`, "phc_test\nkey"]) {
    const result = configuration({ AMW_POSTHOG_KEY: key });
    assert.equal(result.error, "AMW_POSTHOG_KEY must match ^phc_[A-Za-z0-9]+$");
    assert.equal(result.markup, undefined);
  }
});

test("PostHog config rejects every explicitly configured host except the pinned US host", () => {
  const key = freshKey();
  for (const host of [
    "", "  ", "https://eu.i.posthog.com", "http://us.i.posthog.com", "https://us.i.posthog.com/extra",
    "https://us.i.posthog.com//", "https://us.i.posthog.com?query=value",
    "https://us.i.posthog.com#fragment", "https://user@us.i.posthog.com",
    "https://us.i.posthog.com:443", "https://us.i.posthog.com.example.org",
  ]) {
    const result = configuration({ AMW_POSTHOG_KEY: key, AMW_POSTHOG_HOST: host });
    assert.match(result.error, /AMW_POSTHOG_HOST/);
    assert.equal(result.error.includes(key), false);
    assert.equal(result.markup, undefined);
  }
});

for (const [label, key] of [["phx_ key", "phx_invalidfixture"], ["arbitrary string", "not a PostHog capture key"]]) {
  test(`full build rejects a ${label} before emitting HTML`, () => {
    build({ AMW_POSTHOG_KEY: key, AMW_POSTHOG_HOST: host }, (output, result) => {
      assert.match(result.stderr, /site build blocked: AMW_POSTHOG_KEY/);
      assert.equal(`${result.stdout}${result.stderr}`.includes(key), false);
      assert.equal(existsSync(output), false);
    }, 2);
  });
}

test("full build rejects a non-US PostHog host before emitting HTML", () => {
  const key = freshKey();
  build({ AMW_POSTHOG_KEY: key, AMW_POSTHOG_HOST: "https://eu.i.posthog.com" }, (output, result) => {
    assert.match(result.stderr, /site build blocked: AMW_POSTHOG_HOST/);
    assert.equal(`${result.stdout}${result.stderr}`.includes(key), false);
    assert.equal(existsSync(output), false);
  }, 2);
});

test("all page templates use current assets and explicit safe tracked CTA identifiers", () => {
  for (const relative of [...sourcePages, "partials/nav.html", "partials/footer.html"]) {
    const markup = readFileSync(join(siteRoot, relative), "utf8");
    assert.doesNotMatch(markup, /VERCEL_ANALYTICS|va-init\.js|\/_vercel\/insights/);
    if (sourcePages.includes(relative)) {
      assert.match(markup, /@@POSTHOG_CONFIG@@/);
      assert.match(markup, /\/analytics\.js\?v=posthog-2/);
      assert.match(markup, /\/utm-capture\.js\?v=utm-2/);
    }
    for (const [tag] of markup.matchAll(/<[^>]+data-analytics-event="[^"]+"[^>]*>/g)) {
      assert.match(tag, /data-cta-id="[a-z][a-z0-9-]*"/, `${relative}: ${tag}`);
      assert.match(tag, /data-analytics-event="(?:email_click|demo_key_click|proof_click|booking_click)"/);
    }
  }
});

test("full unconfigured build includes no analytics loader even with the old flag set", () => {
  build({ PUBLIC_ENABLE_VERCEL_ANALYTICS: "true" }, (output) => {
    const pages = htmlFiles(output);
    assert.ok(pages.some((path) => path.includes("/docs/")));
    assert.ok(pages.length > sourcePages.length);
    for (const path of pages) {
      const markup = readFileSync(path, "utf8");
      assert.doesNotMatch(markup, /amw-posthog-key|amw-posthog-host|@@POSTHOG_CONFIG@@/);
      assert.doesNotMatch(markup, /va-init\.js|\/_vercel\/insights|<script[^>]+src="[^"]*posthog\.js/);
      assert.match(markup, /\/analytics\.js\?v=posthog-2/);
      assert.match(markup, /\/utm-capture\.js\?v=utm-2/);
    }
    assert.equal(existsSync(join(output, "va-init.js")), false);
    assert.equal(existsSync(join(output, "posthog.js")), true);
  });
});

test("full configured build adds only validated US PostHog metadata to every page", () => {
  for (const environment of [{}, { AMW_POSTHOG_HOST: host }, { AMW_POSTHOG_HOST: `${host}/` }]) {
    const key = freshKey();
    build({ AMW_POSTHOG_KEY: key, ...environment }, (output) => {
      for (const path of htmlFiles(output)) {
        const markup = readFileSync(path, "utf8");
        assert.equal(markup.match(/name="amw-posthog-key"/g)?.length, 1);
        assert.equal(markup.match(/name="amw-posthog-host"/g)?.length, 1);
        assert.ok(markup.includes(`content="${key}"`));
        assert.ok(markup.includes(`content="${host}"`));
        assert.doesNotMatch(markup, /@@POSTHOG_CONFIG@@|va-init\.js|\/_vercel\/insights/);
        assert.doesNotMatch(markup, /<script[^>]+src="https?:/);
      }
      for (const relative of ["analytics.js", "posthog.js", "utm-capture.js"]) {
        assert.equal(readFileSync(join(output, relative), "utf8").includes(key), false);
      }
    });
  }
});

test("CSP permits only the pinned US PostHog ingestion host with same-origin scripts", () => {
  const config = JSON.parse(readFileSync(join(siteRoot, "vercel.json"), "utf8"));
  const csp = config.headers.flatMap((entry) => entry.headers).find((entry) => entry.key === "Content-Security-Policy").value;
  assert.match(csp, /(?:^|; )script-src 'self';/);
  assert.doesNotMatch(csp, /vercel-insights|unsafe-inline|\*/);
  const connect = csp.split("; ").find((directive) => directive.startsWith("connect-src ")).split(" ");
  assert.deepEqual(connect.filter((value) => value.includes("posthog")), [host]);
  const cache = config.headers.find((entry) => entry.source.includes("analytics.js"));
  assert.ok(cache.source.includes("posthog.js"));
  assert.equal(cache.source.includes("va-init.js"), false);
});
