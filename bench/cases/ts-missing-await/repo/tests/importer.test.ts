import { test } from "node:test";
import assert from "node:assert/strict";
import { Store } from "../src/store.ts";
import { importAll } from "../src/importer.ts";

test("importing nothing", async () => {
  assert.equal(await importAll(new Store(), []), 0);
});
