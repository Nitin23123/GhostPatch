import { test } from "node:test";
import assert from "node:assert/strict";
import { shipping } from "../src/shipping.ts";

test("big orders ship free", () => {
  assert.equal(shipping(60), 0);
});
