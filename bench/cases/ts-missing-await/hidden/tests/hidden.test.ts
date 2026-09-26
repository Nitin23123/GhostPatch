import { test } from "node:test";
import assert from "node:assert/strict";
import { Store } from "../src/store.ts";
import { importAll } from "../src/importer.ts";

test("hidden: every row is saved before importAll returns", async () => {
  const store = new Store();
  const imported = await importAll(store, [["a", 1], ["b", 2], ["c", 3]]);
  assert.equal(imported, 3);
  assert.equal(store.count(), 3);
});
