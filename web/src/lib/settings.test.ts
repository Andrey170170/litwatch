import { describe, expect, it } from "vitest";
import type { Schemas } from "@/api/client";
import { acknowledged, answerOf, differs, labelled, level, noted, openSplits, runFailed, savedLine, sessionRow, settle, settleFromPaper, spendBar, spendTotals, toAcknowledge, toToml, took, waiting, type Quality, type Seen, type Session, type SessionItem } from "./settings";

const night: Quality["night"] = { run: 1, error: null, splits: [], awaiting_classification: [], analysis_failed: [], ungraded: [], fulltext: [], digests: [] };
const pass = { id: 7, paper_id: "3", revision: 1, title: "t", kind: "brief", state: "failed", failed_runs: 3, last_error: null, queued_at: "2026-10-01", since: "2026-10-03T03:00:00Z" } as const;
const stuck = { paper_id: "1", revision: 1, title: "t", failed_runs: 3, last_error: null, failed_at: "2026-10-15T03:00:00Z" };
const split = { paper_id: "1", revision: 1, topic: "a", depth: "focus", fit: "related", reason: "", split: true, escalated: false, one_lab: false, opinions: {}, recorded_at: "", title: "t", label: null } as const;

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

describe("toAcknowledge and acknowledged", () => {
  const digest = { id: 4, topic: "a", period: "day", day: "2026-10-08", error: "timed out", written_at: "" } as const;
  const q = { night: { ...night, splits: [split], awaiting_classification: [{ ...stuck, paper_id: "5" }], ungraded: [{ ...stuck, topic: "a" }, { ...stuck, topic: "b", revision: 2 }], analysis_failed: [stuck], fulltext: [pass], digests: [digest] } } as Quality;
  it("names a group's rows as shown, never a split, nor what is settled or acknowledged already", () => {
    expect(toAcknowledge(q, ["ungraded"])).toEqual({
      steps: [
        { step: "grading", paper_id: "1", revision: 1, topic: "a", failed_at: "2026-10-15T03:00:00Z" },
        { step: "grading", paper_id: "1", revision: 2, topic: "b", failed_at: "2026-10-15T03:00:00Z" },
      ],
      passes: [],
      digests: [],
    });
    const later = settle(acknowledged(q, { steps: [], passes: [{ id: 7, since: "2026-10-03T03:00:00+00:00" }], digests: [] }, "2026-10-19T10:00:00Z"), "analysis_failed", { paper_id: "1" }, "Tried again");
    expect(toAcknowledge(later, ["awaiting_classification", "ungraded", "analysis_failed", "fulltext", "digests"])).toMatchObject({ passes: [], digests: [4] });
    expect(toAcknowledge(later, ["analysis_failed"]).steps).toEqual([]);
  });
  it("marks only the rows named as shown, in place, and they stop counting as waiting", () => {
    const failed = "2026-10-15T03:00:00.000+00:00";
    const marked: Seen = {
      steps: [
        { step: "grading", paper_id: "1", revision: 1, topic: "a", failed_at: failed },
        { step: "grading", paper_id: "1", revision: 2, topic: "b", failed_at: "2026-10-18T03:00:00Z" },
        { step: "analysis", paper_id: "5", revision: 1, topic: null, failed_at: failed },
      ],
      passes: [],
      digests: [4],
    };
    const after = acknowledged(q, marked, "2026-10-19T10:00:00Z");
    // Topic b's row shows an earlier failure than the one named: not this row.
    expect(after.night.ungraded.map((s) => s.acknowledged_at ?? null)).toEqual(["2026-10-19T10:00:00Z", null]);
    // The step names the list: paper 5 waits on classification, not analysis.
    expect(after.night.awaiting_classification[0]!.acknowledged_at).toBeUndefined();
    expect(after.night.digests[0]!.acknowledged_at).toBe("2026-10-19T10:00:00Z");
    expect(waiting(q) - waiting(after)).toBe(2);
  });
  it("leaves a row settled on this visit where it is", () => {
    const tried = settle(q, "fulltext", { paper_id: "3" }, "Running");
    const after = acknowledged(tried, { steps: [], passes: [{ id: 7, since: pass.since }], digests: [] }, "2026-10-19T10:00:00Z");
    expect([after.night.fulltext[0]!.settled, after.night.fulltext[0]!.acknowledged_at]).toEqual(["Running", undefined]);
  });
});

describe("settleFromPaper", () => {
  const q = { night: { ...night, splits: [split, { ...split, topic: "b" }], ungraded: [{ ...stuck, topic: "a" }], analysis_failed: [stuck], fulltext: [pass] } } as Quality;
  const topic = (slug: string, label?: string) => ({ topic: { slug, name: slug, depth: "focus", monogram: "A", colour: "#fff" }, fit: "related", label: label ? { value: label } : undefined });
  const paper = (id: string, states: object[], topics: object[] = [], revision = 1) => ({ id, revision, states, topics }) as unknown as Schemas["Paper"];
  it("settles the items whose open state the paper's page no longer carries, a split with its label", () => {
    // Labelled on topic a; still split on b; the ungraded grading done; analysis and the pass still open.
    const after = settleFromPaper(q, paper("1", [{ kind: "split", topic: "b" }, { kind: "analysis_stopped" }], [topic("a", "central"), topic("b")]));
    expect(after.night.splits.map((s) => s.settled)).toEqual(["central", undefined]);
    expect(after.night.ungraded[0]!.settled).toBe("Settled on the paper's page.");
    expect(after.night.analysis_failed[0]!.settled).toBeUndefined();
    expect(after.night.fulltext[0]!.settled).toBeUndefined();
    expect(waiting(after)).toBe(3);
  });
  it("leaves the rows for another revision of the paper as they are", () => {
    // The page acted on revision 2 (a rewrite since the night); the rows are the night's, on revision 1: not these.
    const after = settleFromPaper(q, paper("1", [], [topic("a", "central")], 2));
    expect(after).toEqual(q);
  });
  it("keeps a failed Deep pass open while the paper's page carries it as card_failed", () => {
    const deep = { night: { ...night, fulltext: [{ ...pass, kind: "deep" as const }] } } as Quality;
    const after = settleFromPaper(deep, paper("3", [{ kind: "card_failed", pass_id: 7 }]));
    expect(after.night.fulltext[0]!.settled).toBeUndefined();
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
  it("reads a grade from one lab by its own state", () => {
    const one = { night: { ...night, splits: [{ ...split, split: false, one_lab: true }] } } as Quality;
    expect(settleFromPaper(one, paper("1", [{ kind: "one_lab", topic: "a" }], [topic("a")])).night.splits[0]!.settled).toBeUndefined();
    expect(settleFromPaper(one, paper("1", [], [topic("a", "central")])).night.splits[0]!.settled).toBe("central");
  });
});

describe("openSplits", () => {
  it("groups by the escalation flag, drops splits labelled before the visit and keeps those settled on it", () => {
    const area = { ...split, paper_id: "2", depth: "area", split: true, escalated: true } as const;
    const { splits, escalations } = openSplits([split, area, { ...split, paper_id: "3", label: { value: "none" } }, { ...split, paper_id: "4", label: { value: "none" }, settled: "none" }]);
    expect(splits.map((s) => s.paper_id)).toEqual(["1", "4"]);
    expect(escalations.map((s) => s.paper_id)).toEqual(["2"]);
  });
  it("puts a grade from one lab in its own group, not with the splits", () => {
    const { splits, oneLab } = openSplits([split, { ...split, paper_id: "5", split: false, one_lab: true }]);
    expect(splits.map((s) => s.paper_id)).toEqual(["1"]);
    expect(oneLab.map((s) => s.paper_id)).toEqual(["5"]);
  });
});

describe("answerOf", () => {
  const focus = new Set(["a", "b", "c"]);
  const fresh = { position: 1, paper_id: "1", topic: "a", why: "above", verdict: "related", answer: null, answered_at: null, title: "", abstract: "", authors: [], published: null, fit: { b: "none", c: "related" }, appraisal: null } as SessionItem;
  const done: SessionItem = { ...fresh, answer: "related", answered_at: "2026-10-14", fit: { a: "related", b: "none" }, appraisal: { value: "fine", note: "n" } };
  it("sends the Focus labels changed here and, on a first answer, the drawn topic's Fit; not labels left as they were", () => {
    const { answer, edited, complete } = answerOf(fresh, { fit: { a: "central", b: "none", c: "adjacent", d: "none" } }, focus);
    expect(answer).toEqual({ fit: { a: "central", c: "adjacent" }, value: null, note: null });
    expect([edited, complete]).toEqual([true, true]);
    // A first answer with the drawn topic's label standing and nothing edited: the label as shown, which the server leaves as it is.
    expect(answerOf({ ...fresh, fit: { a: "none" } }, { fit: {} }, focus)).toEqual({ answer: { fit: { a: "none" }, value: null, note: null }, edited: false, complete: true });
  });
  it("needs the drawn topic's Fit on a first answer only, and never writes a label from an appraisal edit", () => {
    expect(answerOf(fresh, { fit: {}, value: "matters" }, focus)).toMatchObject({ edited: true, complete: false });
    expect(answerOf(fresh, { fit: {} }, focus)).toMatchObject({ edited: false, complete: false });
    // Answered before, the label since taken back: the recorded answer is history, a note edit sends no Fit.
    const taken: SessionItem = { ...done, fit: { b: "none" } };
    expect(answerOf(taken, { fit: {}, note: "newer" }, focus)).toEqual({ answer: { fit: {}, value: "fine", note: "newer" }, edited: true, complete: true });
    // Chosen again, explicitly, it is a label.
    expect(answerOf(taken, { fit: { a: "related" } }, focus).answer.fit).toEqual({ a: "related" });
  });
  it("follows the refreshed item where nothing was edited, and reads an edit back to the baseline as no edit", () => {
    const refreshed: SessionItem = { ...done, fit: { a: "none", b: "none" }, appraisal: { value: "matters", note: null } };
    const { answer, edited } = answerOf(refreshed, { fit: {}, note: "" }, focus);
    expect([answer.fit, answer.value, answer.note, edited]).toEqual([{}, "matters", null, false]);
    expect(answerOf(refreshed, { fit: { a: "none" }, value: "matters" }, focus).edited).toBe(false);
    expect(answerOf(refreshed, { fit: { a: "central" } }, focus).answer.fit).toEqual({ a: "central" });
    // Cleared: the value null is an edit against a value standing.
    expect(answerOf(refreshed, { fit: {}, value: null }, focus)).toMatchObject({ answer: { value: null }, edited: true });
  });
});

describe("sessionRow", () => {
  const row = { id: 3, seed: 7, drawn_at: "2026-10-14", papers: 1, answered: 0, differ: 0 };
  const item = (position: number, answer: SessionItem["answer"] = null): SessionItem => ({ position, paper_id: String(position), topic: "a", why: "above", verdict: "related", answer, answered_at: answer ? "2026-10-15" : null, title: "", abstract: "", authors: [], published: null, fit: {}, appraisal: null });
  const session = (...items: SessionItem[]): Session => ({ id: 3, seed: 7, drawn_at: "2026-10-14", items });
  it("reads the counts from the session as it is: an answer, a change of it, unsure and papers added count once", () => {
    expect(sessionRow(row, session(item(1, "none"), item(2)))).toEqual({ ...row, papers: 2, answered: 1, differ: 1 });
    expect(sessionRow(sessionRow(row, session(item(1, "none"))), session(item(1, "related")))).toEqual({ ...row, papers: 1, answered: 1, differ: 0 });
    expect(sessionRow(row, session(item(1, "unsure"), item(2, "central"), item(3)))).toEqual({ ...row, papers: 3, answered: 2, differ: 1 });
  });
});

describe("labelled and noted", () => {
  const q = { night: { ...night, splits: [split, { ...split, topic: "b" }], digests: [{ id: 4, topic: "a", period: "day", day: "2026-10-08", error: "x", written_at: "" }] } } as Quality;
  it("settles the split with its label, replaces it with a changed one, and lets a split taken back wait again, fetched labelled or not", () => {
    const one = labelled(q, "1", "a", "related", 1);
    expect(one.night.splits.map((s) => s.settled)).toEqual(["related", undefined]);
    expect(labelled(one, "1", "a", "central", 1).night.splits[0]!.settled).toBe("central");
    const back = labelled(one, "1", "a", null, 1);
    expect([back.night.splits[0]!.settled, back.night.splits[0]!.label, waiting(back)]).toEqual([undefined, null, 3]);
    const fetchedLabelled = { night: { ...night, splits: [{ ...split, label: { value: "none" } }] } } as Quality;
    expect(labelled(fetchedLabelled, "1", "a", null, 1).night.splits[0]!.label).toBeNull();
    // A row fetched labelled is off the visit's list; labelled again elsewhere, it stays off.
    expect(labelled(fetchedLabelled, "1", "a", "central", 1)).toEqual(fetchedLabelled);
    expect(labelled(q, "9", "a", "none", 1)).toEqual(q);
  });
  it("settles only the row for the revision the label was given on", () => {
    expect(labelled(q, "1", "a", "central", 2)).toEqual(q);
  });
  it("keeps what a write-again said on the digest's row, which still waits", () => {
    const after = noted(q, 4, "Failed again: x.");
    expect(after.night.digests[0]!.note).toBe("Failed again: x.");
    expect(waiting(after)).toBe(3);
  });
  it("keeps a settlement's first word", () => {
    const first = settle(q, "splits", { paper_id: "1", topic: "a" }, "first");
    expect(settle(first, "splits", { paper_id: "1", topic: "a" }, "second").night.splits[0]!.settled).toBe("first");
    // A revision names the text the action was for: a row for another revision is not the one settled.
    const stuckQ = { night: { ...night, analysis_failed: [stuck, { ...stuck, revision: 2 }] } } as Quality;
    expect(settle(stuckQ, "analysis_failed", { paper_id: "1", revision: 2 }, "done").night.analysis_failed.map((s) => s.settled)).toEqual([undefined, "done"]);
  });
});

describe("runFailed", () => {
  const run = { id: 1, kind: "nightly", started_at: "", finished_at: "", cost: 0, error: null, counts: null, fetched: 0, inserted: 0, updated: 0 };
  it("names the error, else the steps that failed on some papers, else nothing", () => {
    expect(runFailed({ ...run, error: "boom" })).toBe("boom");
    expect(runFailed({ ...run, counts: { grading_failed: 20, digests_failed: 2 } })).toBe("20 grading, 2 digests failed");
    expect(runFailed({ ...run, counts: { classified: 3 } })).toBe("");
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
    expect(text).toBe('debug = false\n\n[budget]\nanthropic = 100\n\n[models.judge]\nprovider = "anthropic"\nmodel = "x"\n\n[intake]\ncategories = ["cs.CL", "cs.LG"]\n');
  });
  it("writes an array of tables as repeated headers", () => {
    expect(toToml({ schedule: { jobs: [{ kind: "night", cron: "0 3 * * *" }] } })).toBe('[[schedule.jobs]]\nkind = "night"\ncron = "0 3 * * *"\n');
  });
});
