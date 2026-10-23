/** The re-run offer (DESIGN.md, "The re-run offer"): after a topic is saved, in place of Save and Cancel, the spec's
 * scoped re-runs as rows to tick, each with its papers and estimate, a ceiling, then Not now and Run. Nothing runs by
 * itself: Not now keeps the re-run under Jobs' Not run. */
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { rerunOfferQuery, scheduleQuery } from "@/api/queries";
import { useRerun } from "@/api/mutations";
import { ceilingOf, dayIn, FIRST_SCOPES, type RerunScope, runLabel, SCOPES } from "@/lib/jobs";
import { usd } from "@/lib/settings";
import { Failed } from "./parts";

export function RerunOffer({ slug, line, onDone }: { slug: string; line: string; onDone: () => void }) {
  const zone = useQuery(scheduleQuery).data?.zone;
  const [scopes, setScopes] = useState<RerunScope[]>(FIRST_SCOPES);
  // The date range: the last 30 days in the schedule's zone until the owner picks others.
  const [since, setSince] = useState<string | null>(null);
  const [until, setUntil] = useState<string | null>(null);
  const range = { since: since ?? dayIn(zone, 30), until: until ?? dayIn(zone) };
  const [typed, setTyped] = useState("$5.00");
  const ceiling = ceilingOf(typed);
  const dated = scopes.includes("range");
  const offer = useQuery({
    ...rerunOfferQuery(slug, scopes, dated ? range.since : null, dated ? range.until : null),
    placeholderData: (previous) => previous,
  });
  const run = useRerun();
  const o = offer.data;
  const rows = new Map(o?.rows.map((r) => [r.scope, r]));
  const toggle = (s: RerunScope) => setScopes((on) => (on.includes(s) ? on.filter((x) => x !== s) : SCOPES.map(([k]) => k).filter((k) => k === s || on.includes(k))));
  const send = (go: boolean) =>
    ceiling != null &&
    run.mutate(
      { topic: slug, scopes, since: dated ? range.since : null, until: dated ? range.until : null, ceiling, run: go },
      { onSuccess: onDone },
    );
  // Run and Not now need a scope that names some paper (the counts as read for the ticks shown) and a ceiling.
  const ready = !!o && !offer.isPlaceholderData && o.papers > 0 && ceiling != null && !run.isPending;
  return (
    <div className="offer">
      <div className="oh">{line}</div>
      <div className="nsub">Re-run it now on:</div>
      {SCOPES.map(([scope, label, aside]) => {
        const on = scopes.includes(scope);
        const row = rows.get(scope);
        return (
          <div key={scope}>
            <button className={`ofr${on ? " on" : ""}`} role="checkbox" aria-checked={on} onClick={() => toggle(scope)}>
              <span className="box" aria-hidden>
                {on ? "✓" : ""}
              </span>
              <span className="ol">
                {label}
                {aside && <span className="faint"> · {aside}</span>}
              </span>
              <span className="mono">{row ? row.papers.toLocaleString("en") : "–"}</span>
              <span className="mono faint">{row?.estimate != null ? `≈ ${usd(row.estimate)}` : "–"}</span>
            </button>
            {scope === "range" && on && (
              <div className="ofd">
                <input type="date" className="tin sm mono" aria-label="From" value={range.since} max={range.until} onChange={(e) => e.target.value && setSince(e.target.value)} />
                <span className="faint">to</span>
                <input type="date" className="tin sm mono" aria-label="To" value={range.until} min={range.since} onChange={(e) => e.target.value && setUntil(e.target.value)} />
              </div>
            )}
          </div>
        );
      })}
      <Failed q={offer} what="the re-run's counts" />
      {o && o.missing.length > 0 && <div className="ofn">No estimate: {o.missing.join(", ")} has no calls to go by yet.</div>}
      {o && o.unpriced.length > 0 && <div className="ofn">No price bound, so not sent under a ceiling: {o.unpriced.join("; ")}.</div>}
      <div className="nact">
        <label className="faint ofc">
          Stop at
          <input className="tin sm mono" value={typed} onChange={(e) => setTyped(e.target.value)} aria-label="Stop at" aria-invalid={ceiling == null} />
        </label>
        <span className="sp" />
        <button className="btn" disabled={!ready} onClick={() => send(false)}>
          Not now
        </button>
        <button className="btn pri" disabled={!ready} title={ceiling == null ? "The ceiling is an amount in dollars" : undefined} onClick={() => send(true)}>
          {runLabel(o)}
        </button>
      </div>
    </div>
  );
}
