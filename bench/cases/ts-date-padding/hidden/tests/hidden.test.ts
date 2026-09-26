import { test } from "node:test";
import assert from "node:assert/strict";
import { isoDate } from "../src/dates.ts";
import { invoiceName } from "../src/invoice.ts";

test("hidden: padded", () => {
  assert.equal(invoiceName(new Date(2024, 2, 5)), "invoice-2024-03-05.pdf");
  assert.equal(isoDate(new Date(2024, 0, 9)), "2024-01-09");
});
