import { describe, expect, it } from "vitest";
import { fireWords } from "./schedule";

const DENVER = "America/Denver";
// Monday 12 October 2026, 08:00 in Denver (MDT, UTC-6).
const monday = new Date("2026-10-12T14:00:00Z");

describe("when a job next fires, in words", () => {
  it("says today, tomorrow, a weekday within the week, else the date", () => {
    expect(fireWords("2026-10-12T18:00:00-06:00", DENVER, monday)).toBe("today at 18:00");
    expect(fireWords("2026-10-13T06:00:00-06:00", DENVER, monday)).toBe("tomorrow at 06:00");
    expect(fireWords("2026-10-15T06:00:00-06:00", DENVER, monday)).toBe("Thursday at 06:00");
  });

  it("does not call next week's Monday today", () => {
    expect(fireWords("2026-10-19T07:00:00-06:00", DENVER, monday)).toBe("19 October at 07:00");
  });

  it("reads the calendar in the schedule's zone, not UTC", () => {
    // 23:30 Monday in Denver is already Tuesday in UTC; the 06:00 fire is still "tomorrow".
    const lateMonday = new Date("2026-10-13T05:30:00Z");
    expect(fireWords("2026-10-13T06:00:00-06:00", DENVER, lateMonday)).toBe("tomorrow at 06:00");
    // The fire's clock time is the zone's, whatever the browser's.
    expect(fireWords("2026-10-13T12:00:00Z", DENVER, monday)).toBe("tomorrow at 06:00");
  });
});
