import { describe, expect, it } from "vitest";
import { cronWords, nextFire } from "./cron";

describe("the schedule in words", () => {
  it("reads the nightly and weekly forms", () => {
    expect(cronWords("0 6 * * *")).toBe("every day at 06:00");
    expect(cronWords("30 7 * * 1")).toBe("on Mondays at 07:30");
    expect(cronWords("0 7 * * 7")).toBe("on Sundays at 07:00");
  });

  it("shows any other form as written", () => {
    expect(cronWords("*/15 * * * *")).toBe("*/15 * * * *");
    expect(cronWords("0 6 1 * *")).toBe("0 6 1 * *");
  });

  it("finds the next fire", () => {
    const monday = new Date(2026, 9, 12, 8, 0); // a Monday, after 07:00
    expect(nextFire("0 6 * * *", monday)).toEqual(new Date(2026, 9, 13, 6, 0));
    expect(nextFire("0 7 * * 1", monday)).toEqual(new Date(2026, 9, 19, 7, 0));
    expect(nextFire("*/15 * * * *", monday)).toBeNull();
  });
});
