import { test } from "node:test";
import assert from "node:assert/strict";
import { verifyBundle } from "@agent-middleware/verify";

test("package name imports the built verifier", () => {
  assert.match(import.meta.resolve("@agent-middleware/verify"), /\/dist\/verify\.js$/);
  assert.equal(verifyBundle({}, new Map()).status, "malformed");
});
