import { describe, expect, it } from "vitest";
import { retryNote } from "./Review";

describe("a failed review's line", () => {
  const now = new Date(2026, 9, 14); // Wednesday 14 October, in the browser's clock

  it("promises a retry only while runs still retry it", () => {
    expect(retryNote("day", "2026-10-10", now)).toBe("; litwatch tries again on the next run.");
    expect(retryNote("day", "2026-09-30", now)).toBe(".");
    expect(retryNote("week", "2026-10-05", now)).toBe("; litwatch tries again on the next run.");
    expect(retryNote("week", "2026-09-21", now)).toBe(".");
    expect(retryNote("week", undefined, now)).toBe(".");
  });
});
