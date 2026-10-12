/**
 * When a scheduled job next fires, in words, read in the schedule's own zone: "today at
 * 06:00", "tomorrow at 07:00", "Thursday at 06:00" within the week, else "19 October at
 * 07:00". The server computes the instant; the browser only formats it.
 */

type Parts = { day: string; time: string; weekday: string; date: string };

function parts(d: Date, zone: string): Parts {
  const read = (opts: Intl.DateTimeFormatOptions) => new Intl.DateTimeFormat("en-GB", { timeZone: zone, ...opts });
  // The calendar date from its parts, since a locale's own pattern is not guaranteed.
  const ymd = Object.fromEntries(
    read({ year: "numeric", month: "2-digit", day: "2-digit" })
      .formatToParts(d)
      .map((p) => [p.type, p.value]),
  );
  return {
    day: `${ymd.year}-${ymd.month}-${ymd.day}`,
    time: read({ hour: "2-digit", minute: "2-digit", hourCycle: "h23" }).format(d),
    weekday: read({ weekday: "long" }).format(d),
    date: read({ day: "numeric", month: "long" }).format(d),
  };
}

/** The UTC offset in minutes an ISO timestamp is written in ("-06:00" → -360, "Z" → 0). */
function offsetOf(iso: string): number {
  const m = /([+-])(\d{2}):?(\d{2})$/.exec(iso);
  return m ? (m[1] === "-" ? -1 : 1) * (Number(m[2]) * 60 + Number(m[3])) : 0;
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
  let fire: Parts;
  let today: string;
  try {
    fire = parts(at, zone);
    today = parts(now, zone).day;
  } catch {
    // A zone Intl does not know (a bare offset): the server wrote the instant in that zone's
    // offset, so shift by it and read the clock as UTC.
    const shift = (d: Date) => new Date(d.getTime() + offsetOf(iso) * 60000);
    fire = parts(shift(at), "UTC");
    today = parts(shift(now), "UTC").day;
  }
  const days = daysBetween(today, fire.day);
  const when = days === 0 ? "today" : days === 1 ? "tomorrow" : days > 1 && days < 7 ? fire.weekday : fire.date;
  return `${when} at ${fire.time}`;
}
