export class Store {
  private items = new Map<string, number>();

  async save(key: string, value: number): Promise<void> {
    await new Promise((resolve) => setTimeout(resolve, 5)); // simulates a database write
    this.items.set(key, value);
  }

  count(): number {
    return this.items.size;
  }
}
