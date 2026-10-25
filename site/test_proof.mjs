import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { runInNewContext } from "node:vm";

const script = readFileSync(new URL("./proof/proof.js", import.meta.url), "utf8");
const publishedBundle = JSON.parse(readFileSync(new URL("./proof/receipt.json", import.meta.url), "utf8"));
const publishedKeys = JSON.parse(readFileSync(new URL("./proof/trust-keys.json", import.meta.url), "utf8"));

async function showProof(bundle, keys) {
  const field = { dataset: { proofField: "kid" }, textContent: "" };
  const status = { textContent: "" };
  const document = {
    querySelectorAll(selector) {
      if (selector === "[data-proof-field]") return [field];
      if (selector === "[data-proof-status]") return [status];
      return [];
    },
  };
  runInNewContext(script, {
    document,
    async fetch(path) {
      return {
        ok: true,
        async json() { return path.includes("receipt") ? bundle : keys; },
      };
    },
  });
  await new Promise((resolve) => setImmediate(resolve));
  return { field, status };
}

test("proof key display and match use the signed kid", async () => {
  const bundle = { ...publishedBundle, kid: "" };
  const { field, status } = await showProof(bundle, publishedKeys);
  assert.equal(field.textContent, JSON.parse(bundle.signing_input).kid);
  assert.equal(status.textContent, "artifacts loaded");
});

test("proof page does not claim a matching key when the snapshot has none", async () => {
  const { field, status } = await showProof(publishedBundle, { ...publishedKeys, keys: [] });
  assert.equal(field.textContent, "not published");
  assert.equal(status.textContent, "not published");
});
