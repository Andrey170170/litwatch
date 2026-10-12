import { describe, expect, it } from "vitest";
import { statusLine } from "./status";

const base = { run_id: 1, started_at: "2026-10-12T06:02:00Z", finished_at: "2026-10-12T06:14:00Z", read: 530 };

describe("the printer's line", () => {
  it("is green and counts the night when quiet", () => {
    expect(statusLine({ ...base, state: "quiet" })).toEqual({ tone: "green", text: "530 read · all quiet" });
  });

  it("is amber with each count that waits for Quality", () => {
    expect(statusLine({ ...base, state: "attention", splits: 2, ungraded: 1, awaiting_triage: 0 })).toEqual({
      tone: "amber",
      text: "530 read · 2 disagreements · 1 ungraded",
    });
    expect(statusLine({ ...base, state: "attention", splits: 1, awaiting_triage: 3 }).text).toBe(
      "530 read · 1 disagreement · 3 awaiting triage",
    );
  });

  it("is red on a failed run and names the day shown", () => {
    expect(statusLine({ ...base, state: "failed", error: "boom", showing: "2026-10-06" })).toEqual({
      tone: "red",
      text: "last night's run failed · showing Tuesday",
    });
    expect(statusLine({ ...base, state: "failed", showing: null }).text).toBe("last night's run failed");
  });

  it("is grey while running and on a fresh install", () => {
    expect(statusLine({ state: "running", started_at: "2026-10-12T06:02:00" })).toEqual({
      tone: "grey",
      text: "running since 06:02",
    });
    expect(statusLine({ state: "none" })).toEqual({ tone: "grey", text: "no run yet" });
  });
});
