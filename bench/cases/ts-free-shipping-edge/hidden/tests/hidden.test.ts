import { test } from "node:test";
import assert from "node:assert/strict";
import { shipping } from "../src/shipping.ts";

test("hidden: the edge", () => {
  assert.equal(shipping(50), 0);
  assert.equal(shipping(49.99), 4.99);
});
