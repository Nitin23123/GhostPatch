export const FREE_FROM = 50;

export function shipping(subtotal: number): number {
  return subtotal >= FREE_FROM ? 0 : 4.99;
}
