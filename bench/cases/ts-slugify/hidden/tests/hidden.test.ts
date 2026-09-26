import { test } from "node:test";
import assert from "node:assert/strict";
import { postUrl, sitemap } from "../src/posts.ts";
import { slugify } from "../src/slug.ts";

test("hidden: punctuation and repeated spaces", () => {
  assert.equal(postUrl({ id: 7, title: "Hello,  World!" }), "/blog/7/hello-world");
});

test("hidden: no leading or trailing hyphens", () => {
  assert.equal(slugify("  Rust & Go: 2026 "), "rust-go-2026");
});

test("hidden: sitemap uses the same rules", () => {
  assert.deepEqual(sitemap([{ id: 2, title: "Why?! Because." }]), ["/blog/2/why-because"]);
});
