import { Cart } from "./cart.ts";

export const FREE_SHIPPING_OVER = 50;
export const SHIPPING_FEE = 4.99;

export function shippingCost(cart: Cart): number {
  return cart.subtotal() >= FREE_SHIPPING_OVER ? 0 : SHIPPING_FEE;
}
