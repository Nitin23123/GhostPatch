export interface Line { name: string; price: string }

export function orderTotal(lines: Line[]): number {
  let sum = 0;
  for (const line of lines) sum += Number(line.price);
  return sum;
}
