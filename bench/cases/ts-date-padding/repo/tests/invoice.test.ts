import { test } from "node:test";
import assert from "node:assert/strict";
import { invoiceName } from "../src/invoice.ts";

test("two-digit dates", () => {
  assert.equal(invoiceName(new Date(2024, 10, 25)), "invoice-2024-11-25.pdf");
});
