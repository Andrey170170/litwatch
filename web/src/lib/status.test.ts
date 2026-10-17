import { describe, expect, it } from "vitest";
import { statusLine, type RunStatus } from "./status";

const base: RunStatus = {
  state: "quiet",
  papers: 212,
  awaiting_classification: 0,
  analysis_failed: 0,
  ungraded: 0,
  fulltext_failed: 0,
  digests_failed: 0,
};

describe("statusLine", () => {
  it("says nothing before the first run, unless something waits on the owner already", () => {
    expect(statusLine({ ...base, state: "none", papers: 0 })).toBeNull();
    expect(statusLine({ ...base, state: "none", papers: 0, digests_failed: 1 })).toMatchObject({ text: "1 failed", tone: "failed", page: "needs" });
  });
  it("shows the count when all is quiet", () => {
    expect(statusLine(base)).toMatchObject({ text: "212", tone: "ok", page: "overview" });
  });
  it("counts papers waiting on the owner as failed, ahead of running and budget", () => {
    const s = statusLine({ ...base, state: "running", waiting: 30, analysis_failed: 2, ungraded: 1, over_budget: ["gist"] });
    expect(s).toMatchObject({ text: "3 failed", tone: "failed", page: "needs" });
    expect(statusLine({ ...base, fulltext_failed: 1 })).toMatchObject({ text: "1 failed", page: "needs" });
    expect(statusLine({ ...base, digests_failed: 1 })).toMatchObject({ text: "1 failed", page: "needs" });
  });
  it("shows progress while running", () => {
    expect(statusLine({ ...base, state: "running", papers: 47, waiting: 165 })).toMatchObject({ text: "47/212", tone: "running" });
  });
  it("names over budget after a quiet run", () => {
    expect(statusLine({ ...base, over_budget: ["deep"] })).toMatchObject({ text: "212 · $ over", tone: "over", page: "runs" });
  });
  it("a failed run outranks everything", () => {
    expect(statusLine({ ...base, state: "failed", ungraded: 4, error: "boom" })).toMatchObject({ text: "run failed", tone: "failed" });
  });
});
