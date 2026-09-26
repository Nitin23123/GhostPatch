import { test } from "node:test";
import assert from "node:assert/strict";
import { discounted } from "../src/pricing.ts";

test("10% off", () => {
  assert.equal(discounted(10, 10), 9);
});
