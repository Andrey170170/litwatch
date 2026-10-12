/**
 * When a scheduled job next fires, in words, read in the schedule's own zone: "today at
 * 06:00", "tomorrow at 07:00", "Thursday at 06:00" within the week, else "19 October at
 * 07:00". The server computes the instant; the browser only formats it.
 */

function parts(d: Date, zone: string): { day: string; time: string; weekday: string; date: string } {
  const read = (opts: Intl.DateTimeFormatOptions, locale = "en-GB") => new Intl.DateTimeFormat(locale, { timeZone: zone, ...opts }).format(d);
  return {
    day: read({ year: "numeric", month: "2-digit", day: "2-digit" }, "en-CA"), // YYYY-MM-DD in the zone
    time: read({ hour: "2-digit", minute: "2-digit", hourCycle: "h23" }),
    weekday: read({ weekday: "long" }),
    date: read({ day: "numeric", month: "long" }),
  };
}

/** Calendar days from `a` to `b`, both `YYYY-MM-DD`. */
function daysBetween(a: string, b: string): number {
  const utc = (s: string) => {
    const [y, m, d] = s.split("-").map(Number);
    return Date.UTC(y ?? 1970, (m ?? 1) - 1, d ?? 1);
  };
  return Math.round((utc(b) - utc(a)) / 86400000);
}

export function fireWords(iso: string, zone: string, now = new Date()): string {
  const at = new Date(iso);
  let fire: ReturnType<typeof parts>;
  let today: string;
  try {
    fire = parts(at, zone);
    today = parts(now, zone).day;
  } catch {
    // A zone Intl does not know (a fixed offset): the browser's own clock has to do.
    const local = Intl.DateTimeFormat().resolvedOptions().timeZone;
    fire = parts(at, local);
    today = parts(now, local).day;
  }
  const days = daysBetween(today, fire.day);
  const when = days === 0 ? "today" : days === 1 ? "tomorrow" : days > 1 && days < 7 ? fire.weekday : fire.date;
  return `${when} at ${fire.time}`;
}
