import { describe, expect, it } from "vitest";
import { authorsShort, compact, dateTime, rowDate, tableDate } from "./format";

describe("format", () => {
  it("prints a calendar day as itself whatever the zone, with the year only when not this year", () => {
    const now = new Date(2026, 9, 15);
    expect(rowDate("2026-10-13", now)).toBe("13 Oct");
    expect(rowDate("2025-01-02", now)).toBe("2 Jan 2025");
  });
  it("gives a table row's date from another year as the year alone, to fit its column", () => {
    const now = new Date(2026, 9, 15);
    expect(tableDate("2026-10-13", now)).toBe("13 Oct");
    expect(tableDate("2025-09-10", now)).toBe("2025");
  });
  it("prints a time in the zone named, wherever the browser is", () => {
    expect(dateTime("2026-10-16T03:00:00-06:00", "America/Denver")).toBe("16 Oct, 03:00");
    expect(dateTime("2026-10-16T03:00:00-06:00", "UTC")).toBe("16 Oct, 09:00");
  });
  it("shows three authors and +N, but all four when there are four", () => {
    expect(authorsShort(["a", "b", "c", "d"])).toEqual({ shown: ["a", "b", "c", "d"], more: 0 });
    expect(authorsShort(["a", "b", "c", "d", "e"])).toEqual({ shown: ["a", "b", "c"], more: 2 });
  });
  it("prints a count at a glance", () => {
    expect([3_141_592, 850_000, 12].map(compact)).toEqual(["3.1M", "850K", "12"]);
  });
});
