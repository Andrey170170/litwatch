/** Dates and names as the pages print them. */

const DAY = new Intl.DateTimeFormat("en-GB", { weekday: "long", day: "numeric", month: "long" });
const DAY_SHORT = new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "long" });
const DAY_YEAR = new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "long", year: "numeric" });
const MONTH_YEAR = new Intl.DateTimeFormat("en-GB", { month: "long", year: "numeric" });
const CELL = new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "short", year: "numeric" });
const TIME = new Intl.DateTimeFormat("en-GB", { hour: "2-digit", minute: "2-digit" });
const WEEKDAY = new Intl.DateTimeFormat("en-GB", { weekday: "long" });

/** A `YYYY-MM-DD` as a local calendar day, so it is not shifted by the zone. */
export function day(iso: string): Date {
  const [y, m, d] = iso.split("-").map(Number);
  return new Date(y ?? 1970, (m ?? 1) - 1, d ?? 1);
}

/** A Date as the local calendar day `YYYY-MM-DD`, comparable with an issue's day. */
export function localDay(d: Date): string {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

export const fmt = {
  /** "Wednesday 8 October" */
  issueDay: (iso: string) => DAY.format(day(iso)),
  /** "Wednesday" */
  weekday: (iso: string) => WEEKDAY.format(day(iso)),
  /** "Wednesday", from a Date in the browser's clock. */
  weekdayOf: (d: Date) => WEEKDAY.format(d),
  /** "8 October" or "8 October 2025" when not this year. */
  published: (iso: string | null | undefined) => {
    if (!iso) return "";
    const d = new Date(iso);
    return d.getFullYear() === new Date().getFullYear() ? DAY_SHORT.format(d) : DAY_YEAR.format(d);
  },
  /** "8 October 2026" */
  full: (iso: string | null | undefined) => (iso ? DAY_YEAR.format(new Date(iso)) : ""),
  /** "February 2025" */
  monthYear: (iso: string | null | undefined) => (iso ? MONTH_YEAR.format(new Date(iso)) : ""),
  /** "21 Aug 2026", for table cells. */
  cell: (iso: string | null | undefined) => (iso ? CELL.format(new Date(iso)) : ""),
  /** "06:02" */
  time: (iso: string | null | undefined) => (iso ? TIME.format(new Date(iso)) : ""),
  /** The week a Monday issue covers: "Week of 6 October". */
  week: (iso: string) => `Week of ${DAY_SHORT.format(day(iso))}`,
};

/** Surnames, "and N more" past the fourth (DESIGN.md "Entry"). */
export function authorsLine(authors: readonly string[], max = 4): string {
  const names = authors.map(surname);
  if (names.length <= max) return names.join(", ");
  return `${names.slice(0, max - 1).join(", ")} and ${names.length - max + 1} more`;
}

export function surname(full: string): string {
  const parts = full.trim().split(/\s+/);
  return parts[parts.length - 1] ?? full;
}

export function plural(n: number, one: string, many = `${one}s`): string {
  return `${n} ${n === 1 ? one : many}`;
}
