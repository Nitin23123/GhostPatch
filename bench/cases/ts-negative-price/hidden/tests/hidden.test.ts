import { test } from "node:test";
import assert from "node:assert/strict";
import { discounted } from "../src/pricing.ts";

test("hidden: never negative", () => {
  assert.equal(discounted(10, 120), 0);
  assert.equal(discounted(10, 100), 0);
  assert.equal(discounted(10, 50), 5);
});
