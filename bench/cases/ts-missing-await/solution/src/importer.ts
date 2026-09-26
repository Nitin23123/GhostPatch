import { Store } from "./store.ts";

export type Row = [key: string, value: number];

export async function importAll(store: Store, rows: Row[]): Promise<number> {
  await Promise.all(rows.map(([key, value]) => store.save(key, value)));
  return store.count();
}
