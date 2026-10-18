import { describe, expect, it } from "vitest";
import { calendarMonths, isIsoDay, listRows, markLine, mondayOf, nextSpan, sections, slotted, tonight, unsettled, weekRange, type HistoryPage, type Mark, type TodayRun } from "./digests";

const focus = { slug: "cotf", name: "CoT faithfulness", depth: "focus", monogram: "CF", colour: "#c026d3" } as const;
const area = { slug: "interp", name: "Interpretability", depth: "area", monogram: "IN", colour: "#4f46e5" } as const;
const counts = { papers: 0, picks: 0, central: 0, related: 0, must_read: 0 };
const mark = (over: Partial<Mark>): Mark => ({ id: 1, topic: focus, period: "day", day: "2026-10-13", state: "written", counts, best_rarity: "rare", ...over });

describe("a Mark's count line", () => {
  it("counts new papers and must reads for Focus, picks for Area, and names the week", () => {
    expect(markLine(mark({ counts: { ...counts, papers: 2, must_read: 1 } }))).toBe("2 new · 1 must read");
    expect(markLine(mark({ topic: area, counts: { ...counts, papers: 11, picks: 1 } }))).toBe("11 new · 1 pick");
    expect(markLine(mark({ topic: area, counts: { ...counts, papers: 11, picks: 2 } }))).toBe("11 new · 2 picks");
    expect(markLine(mark({ period: "week", counts: { ...counts, papers: 7 } }))).toBe("Week · 7 papers");
    expect(markLine(mark({ period: "week", topic: area, counts: { ...counts, papers: 64, picks: 5 } }))).toBe("Week · 5 picks");
    expect(markLine(mark({ state: "nothing_new" }))).toBe("Nothing new");
    expect(markLine(mark({ state: "failed" }))).toBe("Could not be written");
  });
});

describe("days", () => {
  it("takes only dates that exist", () => {
    expect(isIsoDay("2028-02-29")).toBe(true);
    expect(["2026-02-31", "2026-13-01", "2026-2-1", "today", 7].map(isIsoDay)).toEqual([false, false, false, false, false]);
  });
});

describe("today's run", () => {
  const night = { due: new Date(2026, 9, 14, 2).toISOString(), deadline: new Date(2026, 9, 14, 7, 30).toISOString(), papers: 0, total: null, error: null };
  const at = (state: TodayRun["state"], more: Partial<TodayRun> = {}) => tonight({ ...night, state, ...more });
  it("names each state with its tone, the night's times in local time and the papers read so far", () => {
    expect(at("waiting")).toMatchObject({ tag: "Standby", tone: "cy", meta: "run at 02:00" });
    expect(at("running", { papers: 120, total: 480 })).toMatchObject({ tag: "Running", tone: "warn", meta: "120 / 480 papers read", progress: 0.25 });
    expect(at("overrun", { papers: 470, total: 480 })?.line).toBe("The run is past its 07:30 deadline and still going. Digests follow.");
    expect(at("late")).toMatchObject({ tag: "No signal", tone: "sig", line: "The nightly run was due at 02:00 and has not started." });
    // No promise of a retry: a run failing before its digests leaves none failed for the next run to write again.
    expect(at("failed", { error: "anthropic down" })).toMatchObject({ tag: "Run failed", tone: "sig", line: "Today's run failed before writing today's digests." });
    expect(at("done")).toBeNull();
  });
  it("is read again until it is done", () => {
    expect([unsettled({ run: { ...night, state: "running" } }), unsettled({ run: { ...night, state: "done" } }), unsettled({ run: null })]).toEqual([true, false, false]);
  });
});

describe("weeks", () => {
  it("names a week by its days, and finds its Monday", () => {
    expect(weekRange("2026-10-05")).toBe("5–11 Oct");
    expect(weekRange("2026-09-28")).toBe("28 Sept – 4 Oct");
    expect(mondayOf("2026-10-15")).toBe("2026-10-12");
    expect(mondayOf("2026-10-12")).toBe("2026-10-12");
    expect(mondayOf("2026-10-18")).toBe("2026-10-12");
  });
});

describe("a digest's sections", () => {
  const paper = (over: object) => ({ id: "x", title: "t", role: "listed", revision: 1, rarity: "rare", set_aside: false, announced: null, why: null, gist: null, fit: null, grade: null, ...over }) as never;
  it("splits a Focus daily digest into Central and Related, the latter without Gists", () => {
    const out = sections("focus", "day", [paper({ id: "a", fit: "central" }), paper({ id: "b", fit: "related" })]);
    expect(out.map((s) => [s.name, s.papers.map((p) => p.id), s.gist])).toEqual([
      ["Central", ["a"], true],
      ["Related", ["b"], false],
    ]);
  });
  it("gives an Area digest its picks, the papers its text names, and the rest as also due", () => {
    const out = sections("area", "day", [paper({ id: "a" }), paper({ id: "b", role: "pick" }), paper({ id: "c", role: "mentioned" })]);
    expect(out.map((s) => [s.name, s.papers.map((p) => p.id)])).toEqual([
      ["Picks", ["b"]],
      ["Mentioned", ["c"]],
      ["Also due", ["a"]],
    ]);
    expect(sections("field", "week", [paper({ id: "b", role: "pick" })]).map((s) => s.name)).toEqual(["Picks"]);
  });
});

/** Mon 5 Oct to Thu 15 Oct 2026: digests on the weekdays, weeklies for the week of 5 Oct, nothing before. */
function page(): HistoryPage {
  const days = [];
  for (let d = 15; d >= 1; d--) {
    const day = `2026-10-${String(d).padStart(2, "0")}`;
    const weekend = [3, 4, 10, 11].includes(d);
    const before = d < 5;
    days.push({ day, released: !weekend && !before, marks: weekend || before ? [] : [mark({ id: d, day }), mark({ id: 100 + d, day, topic: area, counts: { ...counts, papers: 3 } })] });
  }
  return { days, weeks: [{ monday: "2026-10-05", marks: [mark({ id: 50, period: "week", day: "2026-10-05" })] }, { monday: "2026-10-12", marks: [] }], start: "2026-10-05" };
}

describe("History as a list", () => {
  it("pins this week's weeklies, lists days newest first with a weekend as one line, and stops at the archive's start", () => {
    const rows = listRows([page()], "2026-10-15");
    expect(rows[0]).toMatchObject({ kind: "pin", monday: "2026-10-05" });
    expect(rows.slice(1, 5).map((r) => (r.kind === "day" ? r.day : r.kind))).toEqual(["2026-10-15", "2026-10-14", "2026-10-13", "2026-10-12"]);
    // The pinned week is not repeated after the Monday that wrote it; the weekend is one line, Saturday first.
    expect(rows[5]).toEqual({ kind: "quiet", from: "2026-10-10", to: "2026-10-11" });
    expect(rows.filter((r) => r.kind === "week")).toEqual([]);
    expect(rows.at(-1)).toMatchObject({ kind: "day", day: "2026-10-05" });
    expect(rows.find((r) => r.kind === "day" && r.today)).toMatchObject({ day: "2026-10-15" });
  });
  it("closes an older week with its weeklies after the Monday that wrote them", () => {
    const rows = listRows([page()], "2026-10-22");
    const monday = rows.findIndex((r) => r.kind === "day" && r.day === "2026-10-12");
    expect(rows[monday + 1]).toMatchObject({ kind: "week", monday: "2026-10-05" });
    expect(rows[0]?.kind).toBe("day");
  });
  it("closes the week even after a Monday that released nothing", () => {
    const p = page();
    const monday = p.days.find((d) => d.day === "2026-10-12")!;
    monday.released = false;
    monday.marks = [];
    const rows = listRows([p], "2026-10-22");
    const quiet = rows.findIndex((r) => r.kind === "quiet" && r.from === "2026-10-12");
    expect(rows[quiet + 1]).toMatchObject({ kind: "week", monday: "2026-10-05" });
  });
});

describe("a calendar cell's slots", () => {
  it("keeps every topic in its place, a gap where it has no digest, and a topic no longer followed after them", () => {
    const b = mark({ id: 1, topic: area });
    const gone = mark({ id: 2, topic: { ...area, slug: "gone", name: "Gone" } });
    expect(slotted([gone, b], [focus.slug, area.slug]).map((m) => m?.id ?? null)).toEqual([null, 1, 2]);
  });
});

describe("History as a calendar", () => {
  it("lays weeks into their Monday's month, newest month first, with the weeklies beside the week", () => {
    const months = calendarMonths([page()]);
    expect(months.map((m) => [m.month, m.weeks.map((w) => w.monday)])).toEqual([["2026-10", ["2026-10-05", "2026-10-12"]]]);
    const first = months[0]!.weeks[0]!;
    expect(first.days.map((d) => d?.day)).toEqual(["2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09"]);
    expect(first.weekly.map((m) => m.id)).toEqual([50]);
    expect(months[0]!.weeks[1]!.days[3]?.day).toBe("2026-10-15");
    expect(months[0]!.weeks[1]!.days[4]).toBeNull();
  });
  it("lays out only the weeks read so far, not the archive back to its start", () => {
    // the page's oldest day is Thu 1 Oct: its week, Monday 28 Sept, is the last laid out
    const months = calendarMonths([{ ...page(), start: "2024-01-01" }]);
    expect(months.map((m) => [m.month, m.weeks.map((w) => w.monday)])).toEqual([["2026-10", ["2026-10-05", "2026-10-12"]], ["2026-09", ["2026-09-28"]]]);
  });
  it("reads the next page back only until the oldest digest", () => {
    expect(nextSpan([page()])).toBeUndefined();
    expect(nextSpan([{ ...page(), start: "2026-06-01" }])).toBe("2026-09-30");
    expect(nextSpan([{ ...page(), start: null }])).toBeUndefined();
  });
});
