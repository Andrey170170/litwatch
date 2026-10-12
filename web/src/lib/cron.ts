/**
 * The schedule's cron expressions as words. Only the two forms litwatch's defaults take are
 * read ("M H * * *" and "M H * * D"); anything else is shown as written.
 */

const WEEKDAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];

type Simple = { minute: number; hour: number; weekday: number | null };

function parse(expr: string): Simple | null {
  const f = expr.trim().split(/\s+/);
  if (f.length !== 5 || f[2] !== "*" || f[3] !== "*") return null;
  const [minute, hour] = [Number(f[0]), Number(f[1])];
  if (!Number.isInteger(minute) || !Number.isInteger(hour) || minute < 0 || minute > 59 || hour < 0 || hour > 23) return null;
  if (f[4] === "*") return { minute, hour, weekday: null };
  const d = Number(f[4]);
  if (!Number.isInteger(d) || d < 0 || d > 7) return null;
  return { minute, hour, weekday: d % 7 };
}

const clock = (s: Simple) => `${String(s.hour).padStart(2, "0")}:${String(s.minute).padStart(2, "0")}`;

/** "every day at 06:00", "on Mondays at 07:00", or the expression itself. */
export function cronWords(expr: string): string {
  const s = parse(expr);
  if (!s) return expr;
  return s.weekday === null ? `every day at ${clock(s)}` : `on ${WEEKDAYS[s.weekday]}s at ${clock(s)}`;
}

/** The next fire after `now`, in the browser's clock, or null for a form not read. */
export function nextFire(expr: string, now = new Date()): Date | null {
  const s = parse(expr);
  if (!s) return null;
  for (let i = 0; i < 8; i++) {
    const at = new Date(now.getFullYear(), now.getMonth(), now.getDate() + i, s.hour, s.minute);
    if (at > now && (s.weekday === null || at.getDay() === s.weekday)) return at;
  }
  return null;
}
