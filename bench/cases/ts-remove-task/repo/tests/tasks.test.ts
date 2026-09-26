import { test } from "node:test";
import assert from "node:assert/strict";
import { completeTask } from "../src/tasks.ts";

test("last task", () => {
  assert.deepEqual(completeTask(["a", "b"], 1), ["a"]);
});
