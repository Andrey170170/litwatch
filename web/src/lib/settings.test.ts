import { describe, expect, it } from "vitest";
import type { Schemas } from "@/api/client";
import { answerFits, differs, level, openSplits, savedLine, settle, settleFromPaper, spendBar, spendTotals, toToml, took, waiting, type Quality, type SessionItem } from "./settings";

const night: Quality["night"] = { run: 1, error: null, splits: [], awaiting_classification: [], analysis_failed: [], ungraded: [], fulltext: [], digests: [] };
const pass = { id: 7, paper_id: "3", revision: 1, title: "t", kind: "brief", state: "failed", failed_runs: 3, last_error: null, queued_at: "2026-10-01" } as const;
const stuck = { paper_id: "1", revision: 1, title: "t", failed_runs: 3, last_error: null, failed_at: "2026-10-15" };
const split = { paper_id: "1", topic: "a", depth: "focus", fit: "related", reason: "", split: true, escalated: false, opinions: {}, recorded_at: "", title: "t", label: null } as const;

describe("waiting", () => {
  const digest = { id: 4, topic: "a", period: "day", day: "2026-10-08", error: "timed out", written_at: "" } as const;
  const q = { night: { ...night, splits: [split, { ...split, paper_id: "2", label: { value: "central" } }], ungraded: [stuck], analysis_failed: [stuck], fulltext: [pass, { ...pass, id: 8, kind: "weight" }], digests: [digest, { ...digest, id: 5 }] } } as Quality;
  it("counts unlabelled splits, the stuck steps, the full-text passes and the failed digests, not labelled splits", () => {
    expect(waiting(q)).toBe(7);
  });
  it("drops what this visit settled, which keeps its place", () => {
    const after = settle(settle(settle(settle(q, "splits", { paper_id: "1", topic: "a" }, "related"), "analysis_failed", { paper_id: "1" }, "Tried again"), "fulltext", { paper_id: "3", id: 8 }, "Running"), "digests", { id: 5 }, "Written");
    expect(waiting(after)).toBe(3);
    // A digest settles by its id alone; the other with the same topic stays.
    expect(after.night.digests.map((d) => d.settled)).toEqual([undefined, "Written"]);
    expect(after.night.splits.map((s) => s.settled)).toEqual(["related", undefined]);
    expect(after.night.analysis_failed[0]!.settled).toBe("Tried again");
    expect(after.night.ungraded[0]!.settled).toBeUndefined();
    // Of the paper's two passes, only the one run.
    expect(after.night.fulltext.map((s) => s.settled)).toEqual([undefined, "Running"]);
  });
});

describe("settleFromPaper", () => {
  const q = { night: { ...night, splits: [split, { ...split, topic: "b" }], ungraded: [{ ...stuck, topic: "a" }], analysis_failed: [stuck], fulltext: [pass] } } as Quality;
  const topic = (slug: string, label?: string) => ({ topic: { slug, name: slug, depth: "focus", monogram: "A", colour: "#fff" }, fit: "related", label: label ? { value: label } : undefined });
  const paper = (id: string, states: object[], topics: object[] = []) => ({ id, states, topics }) as unknown as Schemas["Paper"];
  it("settles the items whose open state the paper's page no longer carries, a split with its label", () => {
    // Labelled on topic a; still split on b; the ungraded grading done; analysis and the pass still open.
    const after = settleFromPaper(q, paper("1", [{ kind: "split", topic: "b" }, { kind: "analysis_stopped" }], [topic("a", "central"), topic("b")]));
    expect(after.night.splits.map((s) => s.settled)).toEqual(["central", undefined]);
    expect(after.night.ungraded[0]!.settled).toBe("Settled on the paper's page.");
    expect(after.night.analysis_failed[0]!.settled).toBeUndefined();
    expect(after.night.fulltext[0]!.settled).toBeUndefined();
    expect(waiting(after)).toBe(3);
  });
  it("leaves other papers, settled items and a split gone without a label alone", () => {
    const after = settleFromPaper(settle(q, "ungraded", { paper_id: "1", topic: "a" }, "Graded"), paper("1", [], [topic("a"), topic("b")]));
    expect(after.night.splits.map((s) => s.settled)).toEqual([undefined, undefined]);
    expect(after.night.ungraded[0]!.settled).toBe("Graded");
    expect(settleFromPaper(q, paper("9", []))).toEqual(q);
  });
  it("lets an item wait again when the paper's page carries its state once more: a label taken back", () => {
    const settled = settleFromPaper(q, paper("1", [{ kind: "split", topic: "b" }], [topic("a", "central"), topic("b")]));
    const back = settleFromPaper(settled, paper("1", [{ kind: "split", topic: "a" }, { kind: "split", topic: "b" }], [topic("a"), topic("b")]));
    expect(back.night.splits.map((s) => s.settled)).toEqual([undefined, undefined]);
    // The two splits and the other paper's pass wait; the grading and analysis the page settled stay settled.
    expect(waiting(back)).toBe(3);
  });
  it("follows the label the paper's page carries now: a changed one replaces the result, one taken back reopens a row fetched labelled", () => {
    const changed = settleFromPaper(settleFromPaper(q, paper("1", [], [topic("a", "central")])), paper("1", [], [topic("a", "related")]));
    expect(changed.night.splits[0]!.settled).toBe("related");
    const fetchedLabelled = { night: { ...night, splits: [{ ...split, label: { value: "none" } }] } } as Quality;
    expect(waiting(fetchedLabelled)).toBe(0);
    const back = settleFromPaper(fetchedLabelled, paper("1", [{ kind: "split", topic: "a" }], [topic("a")]));
    expect(back.night.splits[0]!.label).toBeNull();
    expect(waiting(back)).toBe(1);
    // A label changed on the page of a row fetched labelled stays off the visit's list.
    expect(settleFromPaper(fetchedLabelled, paper("1", [], [topic("a", "central")])).night.splits[0]!.settled).toBeUndefined();
  });
  it("reads an escalation by its flag, as the paper's page does, even where the graders also split", () => {
    const both = { night: { ...night, splits: [{ ...split, depth: "area", split: true, escalated: true }] } } as Quality;
    expect(settleFromPaper(both, paper("1", [{ kind: "escalated", topic: "a" }], [topic("a")])).night.splits[0]!.settled).toBeUndefined();
    expect(settleFromPaper(both, paper("1", [], [topic("a", "adjacent")])).night.splits[0]!.settled).toBe("adjacent");
  });
});

describe("openSplits", () => {
  it("groups by the escalation flag, drops splits labelled before the visit and keeps those settled on it", () => {
    const area = { ...split, paper_id: "2", depth: "area", split: true, escalated: true } as const;
    const { splits, escalations } = openSplits([split, area, { ...split, paper_id: "3", label: { value: "none" } }, { ...split, paper_id: "4", label: { value: "none" }, settled: "none" }]);
    expect(splits.map((s) => s.paper_id)).toEqual(["1", "4"]);
    expect(escalations.map((s) => s.paper_id)).toEqual(["2"]);
  });
});

describe("answerFits", () => {
  const item = { position: 1, paper_id: "1", topic: "a", why: "above", verdict: "related", answer: null, answered_at: null, title: "", abstract: "", authors: [], published: null, fit: { b: "none", c: "related" }, appraisal: null } as SessionItem;
  it("sends the drawn topic and the Focus labels changed here, not the ones left as they were", () => {
    expect(answerFits({ a: "central", b: "none", c: "adjacent", d: "none" }, item, new Set(["a", "b", "c"]))).toEqual({ a: "central", c: "adjacent" });
  });
});

describe("level", () => {
  it("colours a share by how good it is, either way up", () => {
    expect([level(0.95), level(0.85), level(0.5)]).toEqual(["ok", "warn", "bad"]);
    expect([level(0.04, { hi: false, good: 0.05, ok: 0.1 }), level(0.08, { hi: false, good: 0.05, ok: 0.1 }), level(0.2, { hi: false, good: 0.05, ok: 0.1 })]).toEqual(["ok", "warn", "bad"]);
  });
});

describe("spendBar", () => {
  it("sets only the budgeted providers' spend against the budget", () => {
    const line = (provider: string, spent: number, budget: number | null) => ({ provider, spent, projected: spent, budget, over: false });
    expect(spendTotals([line("a", 100, 150), line("b", 50, null)])).toEqual({ spent: 100, budget: 150, unbudgeted: 50 });
    expect(spendTotals([line("a", 100, null), line("b", 50, null)])).toEqual({ spent: 150, budget: 0, unbudgeted: 0 });
  });

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
  // The recorded answer is what counts; the current label may have moved on since.
  const item = (why: SessionItem["why"], answer: SessionItem["answer"] = null, verdict: SessionItem["verdict"] = null): SessionItem => ({ position: 1, paper_id: "1", topic: "a", why, verdict, answer, answered_at: answer ? "2026-10-15" : null, title: "", abstract: "", authors: [], published: null, fit: { a: "none" }, appraisal: null });
  it("is another Fit than the pipeline's verdict, or the other side from the draw while the verdict is gone", () => {
    expect(differs(item("above", "adjacent", "related"))).toBe(true);
    expect(differs(item("above", "central", "related"))).toBe(true);
    expect(differs(item("above", "related", "related"))).toBe(false);
    expect(differs(item("split", "none", "central"))).toBe(true);
    expect(differs(item("above", "adjacent"))).toBe(true);
    expect(differs(item("above", "related"))).toBe(false);
    expect(differs(item("below", "central"))).toBe(true);
    expect(differs(item("below", "none"))).toBe(false);
  });
  it("never for unsure, unanswered, or a paper taken for another reason", () => {
    expect(differs(item("above", "unsure", "related"))).toBe(false);
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
    expect(savedLine(t, { ...t, depth: "area" }, 3)).toMatch(/new depth now; the Fits already given stand/);
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
