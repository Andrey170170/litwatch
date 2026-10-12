/**
 * The printer's line (DESIGN.md "Status line"): a tone and a sentence about the latest
 * nightly run. Red wins over amber.
 */
import type { Status } from "@/api/client";
import { fmt } from "./format";

export type Tone = "grey" | "red" | "amber" | "green";

export type StatusLine = { tone: Tone; text: string };

export function statusLine(s: Pick<Status, "state"> & Partial<Status>): StatusLine {
  switch (s.state) {
    case "none":
      return { tone: "grey", text: "no run yet" };
    case "running":
      return { tone: "grey", text: `running since ${fmt.time(s.started_at)}` };
    case "failed":
      return {
        tone: "red",
        text: s.showing ? `last night's run failed · showing ${fmt.weekday(s.showing)}` : "last night's run failed",
      };
    case "attention": {
      const parts = [`${s.read ?? 0} read`];
      if (s.splits) parts.push(`${s.splits} ${s.splits === 1 ? "disagreement" : "disagreements"}`);
      if (s.ungraded) parts.push(`${s.ungraded} ungraded`);
      if (s.awaiting_triage) parts.push(`${s.awaiting_triage} awaiting triage`);
      return { tone: "amber", text: parts.join(" · ") };
    }
    case "quiet":
      return { tone: "green", text: `${s.read ?? 0} read · all quiet` };
  }
}
