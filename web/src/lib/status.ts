/** The run status in the wordmark (DESIGN.md, "Run status"): what it says, its colour, and the Settings page it opens. */
import type { Schemas } from "@/api/client";

export type RunStatus = Schemas["RunStatus"];
export type StatusLine = { text: string; tone: "ok" | "running" | "failed" | "over"; page: "overview" | "needs" | "runs"; title: string };

/** Papers and text passes waiting on the owner after a failure: counted on the status line. */
export function problems(s: RunStatus): number {
  return s.awaiting_classification + s.analysis_failed + s.ungraded + s.fulltext_failed;
}

/** Failed outranks running, running outranks over budget; nothing before the first run. */
export function statusLine(s: RunStatus): StatusLine | null {
  if (s.state === "none") return null;
  if (s.state === "failed")
    return { text: "run failed", tone: "failed", page: "needs", title: s.error ? `Last night's run failed: ${s.error}` : "Last night's run failed" };
  const n = problems(s);
  if (n) return { text: `${n} failed`, tone: "failed", page: "needs", title: `${n} waiting on you after a failure · open Needs you` };
  if (s.state === "running") {
    const total = s.papers + (s.waiting ?? 0);
    return { text: `${s.papers}/${total}`, tone: "running", page: "overview", title: `Tonight's run: ${s.papers} of ${total} papers read` };
  }
  if ((s.over_budget ?? []).length)
    return { text: `${s.papers} · $ over`, tone: "over", page: "runs", title: `${(s.over_budget ?? []).join(", ")} projected over budget · open Runs and cost` };
  return { text: String(s.papers), tone: "ok", page: "overview", title: `${s.papers} papers read last night · all quiet` };
}
