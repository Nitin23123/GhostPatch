import { test } from "node:test";
import assert from "node:assert/strict";
import { orderTotal } from "../src/order.ts";

test("one item", () => {
  assert.equal(orderTotal([{ name: "pen", price: "5" }]), 5);
});
