import { describe, expect, it } from "vitest";
import type { Schemas } from "@/api/client";
import { differs, level, savedLine, settle, spendBar, toToml, took, waiting, type Quality, type SessionItem } from "./settings";

const night: Quality["night"] = { run: 1, error: null, splits: [], awaiting_classification: [], analysis_failed: [], ungraded: [] };
const stuck = { paper_id: "1", revision: 1, title: "t", failed_runs: 3, last_error: null, failed_at: "2026-10-15" };
const split = { paper_id: "1", topic: "a", depth: "focus", fit: "related", reason: "", split: true, escalated: false, opinions: {}, recorded_at: "", title: "t", label: null } as const;

describe("waiting", () => {
  const q = { night: { ...night, splits: [split, { ...split, paper_id: "2", label: { value: "central" } }], ungraded: [stuck], analysis_failed: [stuck] } } as Quality;
  it("counts unlabelled splits and the stuck steps, not labelled ones", () => {
    expect(waiting(q)).toBe(3);
  });
  it("drops what this visit settled, which keeps its place", () => {
    const after = settle(settle(q, "splits", "1", "a", "related"), "analysis_failed", "1", null, "Tried again");
    expect(waiting(after)).toBe(1);
    expect(after.night.splits.map((s) => s.settled)).toEqual(["related", undefined]);
    expect(after.night.analysis_failed[0]!.settled).toBe("Tried again");
    expect(after.night.ungraded[0]!.settled).toBeUndefined();
  });
});

describe("level", () => {
  it("colours a share by how good it is, either way up", () => {
    expect([level(0.95), level(0.85), level(0.5)]).toEqual(["ok", "warn", "bad"]);
    expect([level(0.04, { hi: false, good: 0.05, ok: 0.1 }), level(0.08, { hi: false, good: 0.05, ok: 0.1 }), level(0.2, { hi: false, good: 0.05, ok: 0.1 })]).toEqual(["ok", "warn", "bad"]);
  });
});

describe("spendBar", () => {
  it("fills the bar with the wider of the budget and the projection", () => {
    expect(spendBar({ provider: "a", spent: 25, projected: 50, budget: 100, over: false })).toEqual({ spent: 0.25, projected: 0.5, budget: 1 });
    expect(spendBar({ provider: "a", spent: 60, projected: 120, budget: 100, over: true })).toEqual({ spent: 0.5, projected: 1, budget: 100 / 120 });
    expect(spendBar({ provider: "a", spent: 0, projected: 0, budget: null, over: false })).toEqual({ spent: 0, projected: 0, budget: null });
  });
});

describe("took", () => {
  it("prints minutes, then hours", () => {
    expect(took("2026-10-15T01:00:00Z", "2026-10-15T01:41:30Z")).toBe("42 min");
    expect(took("2026-10-15T01:00:00Z", "2026-10-15T03:05:00Z")).toBe("2 h 05");
    expect(took("2026-10-15T01:00:00Z", null)).toBe("");
  });
});

describe("differs", () => {
  const item = (why: SessionItem["why"], fit?: SessionItem["fit"][string], verdict: SessionItem["verdict"] = null): SessionItem => ({ position: 1, paper_id: "1", topic: "a", why, verdict, answered_at: null, title: "", abstract: "", authors: [], published: null, fit: fit ? { a: fit } : {}, appraisal: null });
  it("is the owner on the other side from the pipeline's Fit, or from the draw while the verdict is gone", () => {
    expect(differs(item("above", "adjacent", "related"))).toBe(true);
    expect(differs(item("above", "central", "related"))).toBe(false);
    expect(differs(item("split", "none", "central"))).toBe(true);
    expect(differs(item("above", "adjacent"))).toBe(true);
    expect(differs(item("above", "related"))).toBe(false);
    expect(differs(item("below", "central"))).toBe(true);
    expect(differs(item("below", "none"))).toBe(false);
  });
  it("never for unsure, unanswered, or a paper taken for another reason", () => {
    expect(differs(item("above", "unsure"))).toBe(false);
    expect(differs(item("above"))).toBe(false);
    expect(differs(item("split", "none"))).toBe(false);
  });
});

describe("savedLine", () => {
  const t: Schemas["Topic"] = { class: "a", depth: "focus", monogram: "A", colour: "#000", status: "active", subject: "s", rubric: "r", interests: "", keywords: ["k"] };
  it("names a new version when what the models read changed", () => {
    expect(savedLine(t, { ...t, subject: "other" }, 3)).toMatch(/version 3/);
    expect(savedLine(t, { ...t, keywords: [] }, 3)).toMatch(/version 3/);
  });
  it("says the shape changed on a change of depth, and only Saved for the look", () => {
    expect(savedLine(t, { ...t, depth: "area" }, 3)).toMatch(/new depth/);
    expect(savedLine(t, { ...t, colour: "#fff" }, 3)).toBe("Saved.");
    expect(savedLine(null, t, 1)).toMatch(/Followed/);
  });
});

describe("toToml", () => {
  it("writes sections with dotted tables and masked strings as they are", () => {
    const text = toToml({ debug: false, budget: { anthropic: 100 }, models: { judge: { provider: "anthropic", model: "x", effort: null } }, intake: { categories: ["cs.CL", "cs.LG"] } });
    expect(text).toBe('debug = false\n\n[budget]\nanthropic = 100\n\n[models.judge]\nprovider = "anthropic"\nmodel = "x"\neffort = ""\n\n[intake]\ncategories = ["cs.CL", "cs.LG"]\n');
  });
  it("writes an array of tables as repeated headers", () => {
    expect(toToml({ schedule: { jobs: [{ kind: "night", cron: "0 3 * * *" }] } })).toBe('[[schedule.jobs]]\nkind = "night"\ncron = "0 3 * * *"\n');
  });
});
