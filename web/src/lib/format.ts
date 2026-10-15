/** Dates and names as the pages print them. */

const ROW = new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "short" });
const ROW_YEAR = new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "short", year: "numeric" });
const TIME = new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });

/** A date-only string is a calendar day, not UTC midnight: parse it as a local day so it prints as itself. */
function parse(iso: string): Date {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(iso);
  return m ? new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3])) : new Date(iso);
}

/** "13 Oct", or "13 Oct 2025" when not this year. */
export function rowDate(iso: string | null | undefined, now = new Date()): string {
  if (!iso) return "";
  const d = parse(iso);
  return d.getFullYear() === now.getFullYear() ? ROW.format(d) : ROW_YEAR.format(d);
}
/** "13 Oct 2026" */
export function fullDate(iso: string | null | undefined): string {
  return iso ? ROW_YEAR.format(parse(iso)) : "";
}
/** "13 Oct, 09:12" */
export function dateTime(iso: string | null | undefined): string {
  return iso ? TIME.format(new Date(iso)) : "";
}

/** The first three authors and "+N" (DESIGN.md, the paper header). */
export function authorsShort(authors: readonly string[], max = 3): { shown: string[]; more: number } {
  if (authors.length <= max + 1) return { shown: [...authors], more: 0 };
  return { shown: authors.slice(0, max), more: authors.length - max };
}

export function plural(n: number, one: string, many = `${one}s`): string {
  return `${n} ${n === 1 ? one : many}`;
}
