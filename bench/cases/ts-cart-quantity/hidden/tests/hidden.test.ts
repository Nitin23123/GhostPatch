import { test } from "node:test";
import assert from "node:assert/strict";
import { Cart } from "../src/cart.ts";
import { receipt } from "../src/checkout.ts";
import { shippingCost } from "../src/shipping.ts";

test("hidden: quantity is charged", () => {
  const cart = new Cart();
  cart.add("mug", 12.5, 3);
  assert.equal(receipt(cart), "3 items, total $42.49");
});

test("hidden: several items can earn free shipping", () => {
  const cart = new Cart();
  cart.add("tea", 30, 2);
  assert.equal(shippingCost(cart), 0);
});
