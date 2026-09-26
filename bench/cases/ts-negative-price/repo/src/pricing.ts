export function discounted(price: number, percent: number): number {
  return price - (price * percent) / 100;
}
