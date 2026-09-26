export interface Line { name: string; price: string }

export function orderTotal(lines: Line[]): number {
  let sum: any = 0;
  for (const line of lines) sum += line.price;
  return Number(sum);
}
