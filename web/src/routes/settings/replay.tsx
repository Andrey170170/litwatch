/** Overview's Run replay (DESIGN.md, "Overview"): what a replay of every cohort's labels would do and cost, a
 * ceiling, then Cancel and Run; one its ceiling stopped goes on instead. A replay queued or running is shown in its
 * place, with the way to Jobs; recall above it follows as the replay concludes (`OverviewPage`). */
import { Link } from "@tanstack/react-router";
import type { UseQueryResult } from "@tanstack/react-query";
import { useState } from "react";
import { useRunReplay } from "@/api/mutations";
import { plural } from "@/lib/format";
import { ceilingOf, type ReplayOffer, replayLine } from "@/lib/jobs";
import { usd } from "@/lib/settings";
import { Failed } from "./parts";

export function RunReplay({ offer }: { offer: UseQueryResult<ReplayOffer> }) {
  const run = useRunReplay();
  const [open, setOpen] = useState(false);
  const [typed, setTyped] = useState("$5.00");
  const o = offer.data;
  if (o?.running)
    return (
      <div className="nsub">
        A replay is {o.running.state === "running" ? "running" : "queued"}; recall follows as it concludes.{" "}
        <Link to="/settings/runs/jobs" className="inl">
          Jobs ›
        </Link>
      </div>
    );
  if (!open)
    return (
      <div className="nact">
        <button className="btn" disabled={!o} onClick={() => setOpen(true)}>
          Run replay
        </button>
        <Failed q={offer} what="what a replay would do" />
      </div>
    );
  const ceiling = ceilingOf(typed);
  // a stopped replay goes on only under a ceiling above what it spent
  const above = ceiling != null && ceiling > (o?.spent ?? 0);
  const ready = !!o && offer.fetchStatus === "idle" && above && o.papers + o.pairs > 0 && !run.isPending;
  return (
    <div className="offer">
      <div className="oh">{o ? replayLine(o) : ""}</div>
      {o && o.left_out > 0 && <div className="ofn">{plural(o.left_out, "labelled paper")} not in the database, left out.</div>}
      {o && o.unpriced.length > 0 && <div className="ofn">No price bound, so not sent under a ceiling: {o.unpriced.join("; ")}.</div>}
      <Failed q={offer} what="what a replay would do" />
      <div className="nact">
        <label className="faint ofc">
          Stop at
          <input className="tin sm mono" value={typed} onChange={(e) => setTyped(e.target.value)} aria-label="Stop at" aria-invalid={!above} />
        </label>
        <span className="sp" />
        <button className="btn" onClick={() => setOpen(false)}>
          Cancel
        </button>
        <button
          className="btn pri"
          disabled={!ready}
          title={!above ? (o?.continues != null ? `The ceiling must be above what it spent, ${usd(o.spent)}` : "The ceiling is an amount in dollars") : undefined}
          onClick={() => run.mutate(ceiling!, { onSuccess: () => setOpen(false) })}
        >
          {o ? `Run · ≈ ${usd(o.estimate)}` : "Run"}
        </button>
      </div>
    </div>
  );
}
