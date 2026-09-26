import { test } from "node:test";
import assert from "node:assert/strict";
import { orderTotal } from "../src/order.ts";

test("hidden: several items", () => {
  assert.equal(orderTotal([{ name: "a", price: "12.50" }, { name: "b", price: "3.00" }]), 15.5);
});

test("hidden: empty order", () => {
  assert.equal(orderTotal([]), 0);
});
