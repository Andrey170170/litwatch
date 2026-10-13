import { describe, expect, it } from "vitest";
import { lastReview } from "./topics";

describe("a card's last review", () => {
  it("reads the date as a local calendar day, whatever the zone", () => {
    // New Year's Day, reviewed that morning: a UTC-midnight reading would say 31 December.
    expect(lastReview({ last_review: "2026-01-01" }, new Date(2026, 0, 1, 9))).toBe("Reviewed Thursday");
    expect(lastReview({ last_review: "2026-01-01" }, new Date(2026, 0, 3, 9))).toBe("Reviewed Thursday");
  });

  it("gives the date past a week, with the year when it is not this one", () => {
    const thisYear = new Date().getFullYear();
    expect(lastReview({ last_review: `${thisYear}-03-02` }, new Date(thisYear, 2, 20))).toBe("Reviewed 2 March");
    expect(lastReview({ last_review: "2025-12-29" }, new Date(2026, 0, 20))).toBe("Reviewed 29 December 2025");
    expect(lastReview({ last_review: null })).toBeNull();
  });
});
