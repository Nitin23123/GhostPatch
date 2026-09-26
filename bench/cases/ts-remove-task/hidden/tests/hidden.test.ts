import { test } from "node:test";
import assert from "node:assert/strict";
import { removeAt } from "../src/list.ts";

test("hidden: middle item", () => {
  const items = ["a", "b", "c", "d"];
  assert.deepEqual(removeAt(items, 1), ["a", "c", "d"]);
  assert.deepEqual(items, ["a", "b", "c", "d"]);
});
