import { Cart, round2 } from "./cart.ts";
import { shippingCost } from "./shipping.ts";

export function orderTotal(cart: Cart): number {
  return round2(cart.subtotal() + shippingCost(cart));
}

export const receipt = (cart: Cart): string =>
  `${cart.itemCount()} items, total $${orderTotal(cart).toFixed(2)}`;
