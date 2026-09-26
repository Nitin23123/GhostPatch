import { describe, test } from "node:test";
import assert from "node:assert/strict";
import { Cart } from "../src/cart.ts";
import { orderTotal, receipt } from "../src/checkout.ts";

describe("checkout", () => {
  test("a small order pays shipping", () => {
    const cart = new Cart();
    cart.add("mug", 12.5);
    assert.equal(orderTotal(cart), 17.49);
  });

  test("the receipt shows the item count and total", () => {
    const cart = new Cart();
    cart.add("tea", 25);
    assert.equal(receipt(cart), "1 items, total $29.99");
  });
});
