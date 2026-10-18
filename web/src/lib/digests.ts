/**
 * The Digest tab's pure rules (DESIGN.md, "Digest"; digests.md, "What the Digest tab shows"): what a Mark's count
 * line says, how days and weeks are named, how History's days are grouped into a list and into calendar months.
 */
import type { Schemas } from "@/api/client";
import { plural } from "./format";

export type Mark = Schemas["DigestMark"];
export type Card = Schemas["DigestCard"];
export type DigestPaper = Schemas["DigestPaper"];
export type HistoryPage = Schemas["History"];
export type HistoryDay = Schemas["HistoryDay"];

// -- days ------------------------------------------------------------------------------------------------------------

/** A calendar day as the API writes it, from a local Date. */
export function isoDay(d: Date): string {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}
/** A date that exists: Date would read 2026-02-31 as a day in March. */
export const isIsoDay = (s: unknown): s is string => typeof s === "string" && /^\d{4}-\d{2}-\d{2}$/.test(s) && isoDay(parseDay(s)) === s;

/** A date-only string as a local day (UTC midnight would print as the day before west of Greenwich). */
export function parseDay(iso: string): Date {
  const [y, m, d] = iso.split("-").map(Number);
  return new Date(y!, m! - 1, d!);
}
export function addDays(iso: string, n: number): string {
  const d = parseDay(iso);
  d.setDate(d.getDate() + n);
  return isoDay(d);
}
/** Monday of the week holding the day. */
export function mondayOf(iso: string): string {
  const d = parseDay(iso);
  return addDays(iso, -((d.getDay() + 6) % 7));
}
export const isWeekend = (iso: string) => [0, 6].includes(parseDay(iso).getDay());

const WEEKDAY = new Intl.DateTimeFormat("en-GB", { weekday: "short", day: "numeric", month: "short" });
const DAY_MONTH = new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "short" });
const MONTH = new Intl.DateTimeFormat("en-GB", { month: "long", year: "numeric" });

/** "Wed 14 Oct" */
export const dayLabel = (iso: string) => WEEKDAY.format(parseDay(iso));
/** "5–11 Oct", or "28 Sep – 4 Oct" across a month's end. */
export function weekRange(monday: string): string {
  const from = parseDay(monday);
  const to = parseDay(addDays(monday, 6));
  return from.getMonth() === to.getMonth() ? `${from.getDate()}–${DAY_MONTH.format(to)}` : `${DAY_MONTH.format(from)} – ${DAY_MONTH.format(to)}`;
}
/** "October 2026" */
export const monthLabel = (iso: string) => MONTH.format(parseDay(iso));

// -- marks -----------------------------------------------------------------------------------------------------------

/** The count line of a Mark ("2 new · 1 must read", "11 new · 1 pick", "Week · 3 picks", "Nothing new"). */
export function markLine(m: Mark): string {
  if (m.state === "failed") return "Could not be written";
  if (m.state === "nothing_new") return "Nothing new";
  const c = m.counts;
  if (m.period === "week") return `Week · ${m.topic.depth === "focus" || !c.picks ? plural(c.papers, "paper") : plural(c.picks, "pick")}`;
  if (m.topic.depth === "focus") return `${c.papers} new${c.must_read ? ` · ${c.must_read} must read` : ""}`;
  return `${c.papers} new${c.picks ? ` · ${plural(c.picks, "pick")}` : ""}`;
}

/** What a calendar bar says beside its badge: the count, "–" for nothing new, "failed". */
export function markShort(m: Mark): string {
  if (m.state === "failed") return "failed";
  return m.state === "nothing_new" ? "–" : String(m.counts.papers);
}

/** "Daily digest · Wed 14 Oct", "Weekly digest · 5–11 Oct". */
export const periodLine = (m: Pick<Mark, "period" | "day">) => (m.period === "week" ? `Weekly digest · ${weekRange(m.day)}` : `Daily digest · ${dayLabel(m.day)}`);

/** Where today's nightly run stands, from the Day (`run`, today only). */
export type TodayRun = Schemas["TodayRun"];

const CLOCK = new Intl.DateTimeFormat("en-GB", { hour: "2-digit", minute: "2-digit" });
const clock = (iso: string) => CLOCK.format(new Date(iso));

/** Today's run as the panel in place of the cards shows it (DESIGN.md, Day): a tag and its tone, one line, and
 * mono meta beside the tag; `progress` while it runs. None once it is done: the day's cards or its line say the rest. */
export function tonight(run: TodayRun): { tag: string; tone: "cy" | "warn" | "sig"; line: string; meta: string; progress?: number } | null {
  const read = run.total ? `${run.papers} / ${run.total} papers read` : `${run.papers} papers read`;
  const progress = run.total ? run.papers / run.total : undefined;
  switch (run.state) {
    case "waiting":
      return { tag: "Standby", tone: "cy", line: "Today's digests arrive with the nightly run.", meta: `run at ${clock(run.due)}` };
    case "running":
      return { tag: "Running", tone: "warn", line: "The nightly run is reading today's papers. Digests follow.", meta: read, progress };
    case "overrun":
      return { tag: "Overrun", tone: "warn", line: `The run is past its ${clock(run.deadline)} deadline and still going. Digests follow.`, meta: read, progress };
    case "late":
      return { tag: "No signal", tone: "sig", line: `The nightly run was due at ${clock(run.due)} and has not started.`, meta: `due ${clock(run.due)}` };
    case "failed":
      return { tag: "Run failed", tone: "sig", line: "Today's run failed before writing today's digests.", meta: `due ${clock(run.due)}` };
    case "done":
      return null;
  }
}

/** A Day is read again every minute while today's run is still to settle. */
export const unsettled = (d: { run?: TodayRun | null } | undefined) => !!d?.run && d.run.state !== "done";

/** The sections a digest's papers fall into, in order (DESIGN.md, "A digest"): picks, then the papers its text
 * names, then the rest, which a Focus digest splits by Fit (Related rows have no Gist). */
export function sections(depth: Schemas["Depth"], period: Schemas["DigestPeriod"], papers: DigestPaper[]): { name: string; papers: DigestPaper[]; gist: boolean }[] {
  const weekly = period === "week";
  const by = (f: (p: DigestPaper) => boolean) => papers.filter(f);
  const out =
    depth === "focus"
      ? [
          { name: "Picks", papers: by((p) => p.role === "pick"), gist: true },
          { name: "Mentioned", papers: by((p) => p.role === "mentioned"), gist: true },
          { name: weekly ? "Central this week" : "Central", papers: by((p) => p.role === "listed" && p.fit !== "related"), gist: true },
          { name: weekly ? "Related this week" : "Related", papers: by((p) => p.role === "listed" && p.fit === "related"), gist: false },
        ]
      : [
          { name: "Picks", papers: by((p) => p.role === "pick"), gist: true },
          { name: "Mentioned", papers: by((p) => p.role === "mentioned"), gist: true },
          { name: weekly ? "Also this week" : "Also due", papers: by((p) => p.role === "listed"), gist: true },
        ];
  return out.filter((s) => s.papers.length);
}

// -- History ---------------------------------------------------------------------------------------------------------

/** One History request covers at most this many days (the API's HISTORY_SPAN is 92). */
export const SPAN = 91;

/** The days of the next page back: from the day before the oldest one read, `SPAN` days back; none once the
 * oldest digest (`start`) is read, or when there is none. */
export function nextSpan(pages: HistoryPage[]): string | undefined {
  const last = pages.at(-1);
  if (!last?.start) return undefined;
  const oldest = last.days.at(-1)?.day;
  return oldest && oldest > last.start ? addDays(oldest, -1) : undefined;
}

export type ListRow =
  | { kind: "pin"; monday: string; marks: Mark[] }
  | { kind: "day"; day: string; marks: Mark[]; today: boolean }
  | { kind: "quiet"; from: string; to: string }
  | { kind: "week"; monday: string; marks: Mark[] };

/** History as a list: the current week's weeklies pinned at the top until the next Monday; then days, newest
 * first, a weekend as one line, each week's weeklies closing the week they cover (after the Monday that wrote
 * them). Days before the oldest digest are left out. */
export function listRows(pages: HistoryPage[], today: string): ListRow[] {
  const start = pages[0]?.start ?? null;
  const weeks = new Map(pages.flatMap((p) => p.weeks).map((w) => [w.monday, w.marks]));
  const days = pages.flatMap((p) => p.days).filter((d) => start && d.day >= start);
  const pinned = addDays(mondayOf(today), -7);
  const rows: ListRow[] = [];
  const pin = weeks.get(pinned);
  if (pin?.length) rows.push({ kind: "pin", monday: pinned, marks: pin });
  for (let i = 0; i < days.length; i++) {
    const d = days[i]!;
    if (!d.released && !d.marks.length) {
      // A weekend is one line; Sunday comes first (newest first), and its Saturday follows.
      const sat = days[i + 1];
      if (isWeekend(d.day) && sat && isWeekend(sat.day) && !sat.released && !sat.marks.length) {
        rows.push({ kind: "quiet", from: sat.day, to: d.day });
        i++;
      } else rows.push({ kind: "quiet", from: d.day, to: d.day });
    } else rows.push({ kind: "day", day: d.day, marks: d.marks, today: d.day === today });
    // A Monday closes the week before with its weeklies, whether or not the Monday itself released anything.
    const covered = addDays(d.day, -7);
    const marks = parseDay(d.day).getDay() === 1 && covered !== pinned ? weeks.get(covered) : undefined;
    if (marks?.length) rows.push({ kind: "week", monday: covered, marks });
  }
  return rows;
}

/** Marks laid into fixed slots (digests.md: a topic is always in the same place): one per topic of `order`, null where
 * the topic has no digest that day, then the Marks of topics no longer in the order. */
export function slotted(marks: Mark[], order: string[]): (Mark | null)[] {
  const by = new Map(marks.map((m) => [m.topic.slug, m]));
  return [...order.map((slug) => by.get(slug) ?? null), ...marks.filter((m) => !order.includes(m.topic.slug))];
}

export type CalWeek = { monday: string; days: (HistoryDay | null)[]; weekly: Mark[] };
export type CalMonth = { month: string; weeks: CalWeek[] };

/** History as a calendar: months, newest first, each its weeks top to bottom (a week belongs to the month of its
 * Monday); a week row holds Monday to Friday (null before the oldest digest) and the week's weekly digests. Only the
 * weeks read so far are laid out: the calendar grows as pages arrive, down to the archive's start. */
export function calendarMonths(pages: HistoryPage[]): CalMonth[] {
  const start = pages[0]?.start ?? null;
  const oldest = pages.at(-1)?.days.at(-1)?.day;
  if (!start || !oldest) return [];
  const weeks = new Map(pages.flatMap((p) => p.weeks).map((w) => [w.monday, w.marks]));
  const days = new Map(pages.flatMap((p) => p.days).map((d) => [d.day, d]));
  const months: CalMonth[] = [];
  const first = mondayOf(oldest > start ? oldest : start);
  const newest = pages[0]?.days[0]?.day;
  for (let monday = newest ? mondayOf(newest) : first; monday >= first; monday = addDays(monday, -7)) {
    const month = monday.slice(0, 7);
    if (months.at(-1)?.month !== month) months.push({ month, weeks: [] });
    const row: CalWeek = { monday, days: [0, 1, 2, 3, 4].map((i) => days.get(addDays(monday, i)) ?? null).map((d) => (d && d.day >= start ? d : null)), weekly: weeks.get(monday) ?? [] };
    months.at(-1)!.weeks.unshift(row);
  }
  return months;
}
