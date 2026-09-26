export interface Line {
  name: string;
  price: number;
  quantity: number;
}

export function round2(value: number): number {
  return Math.round(value * 100) / 100;
}

export class Cart {
  lines: Line[] = [];

  add(name: string, price: number, quantity = 1): void {
    this.lines.push({ name, price, quantity });
  }

  itemCount(): number {
    return this.lines.reduce((count, line) => count + line.quantity, 0);
  }

  subtotal(): number {
    return round2(this.lines.reduce((sum, line) => sum + line.price, 0));
  }
}
