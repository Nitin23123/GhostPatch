import { isoDate } from "./dates.ts";

export function invoiceName(d: Date): string {
  return `invoice-${isoDate(d)}.pdf`;
}
