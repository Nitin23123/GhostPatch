import { test } from "node:test";
import assert from "node:assert/strict";
import { postUrl } from "../src/posts.ts";

test("simple titles", () => {
  assert.equal(postUrl({ id: 1, title: "Hello World" }), "/blog/1/hello-world");
});
