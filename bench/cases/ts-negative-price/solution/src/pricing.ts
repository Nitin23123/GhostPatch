export function discounted(price: number, percent: number): number {
  return Math.max(0, price - (price * percent) / 100);
}
