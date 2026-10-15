import { describe, expect, it } from "vitest";
import { authorsShort, rowDate } from "./format";

describe("format", () => {
  it("prints a calendar day as itself whatever the zone, with the year only when not this year", () => {
    const now = new Date(2026, 9, 15);
    expect(rowDate("2026-10-13", now)).toBe("13 Oct");
    expect(rowDate("2025-01-02", now)).toBe("2 Jan 2025");
  });
  it("shows three authors and +N, but all four when there are four", () => {
    expect(authorsShort(["a", "b", "c", "d"])).toEqual({ shown: ["a", "b", "c", "d"], more: 0 });
    expect(authorsShort(["a", "b", "c", "d", "e"])).toEqual({ shown: ["a", "b", "c"], more: 2 });
  });
});
