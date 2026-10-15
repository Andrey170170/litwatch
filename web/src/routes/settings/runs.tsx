/** Runs and cost (DESIGN.md, "Runs and cost"): spend per provider against its budget, the schedule, the nights
 * as a table, and Jobs, which waits on the jobs API. */
import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { qualityQuery, runsQuery, scheduleQuery } from "@/api/queries";
import { useDesktop } from "@/hooks/useDesktop";
import { dateTime, rowDate } from "@/lib/format";
import { type Run, spendBar, type Spend, took, usd } from "@/lib/settings";
import { Later, Load, PageHead } from "./parts";

function SpendRow({ s }: { s: Spend }) {
  const bar = spendBar(s);
  const pct = (x: number) => `${Math.round(x * 100)}%`;
  return (
    <div className={`spend${s.over ? " past" : ""}`}>
      <span className="pn">{s.provider}</span>
      <span className="sbar" aria-hidden>
        <i className="sp2" style={{ width: pct(bar.projected) }} />
        <i className="sp1" style={{ width: pct(bar.spent) }} />
        {bar.budget != null && <i className="bud" style={{ left: pct(bar.budget) }} />}
      </span>
      <span className="sv2">
        {usd(s.spent)}
        {s.budget != null ? ` of ${usd(s.budget)}` : ""} · <span className={s.over ? "warnt" : "faint"}>projected {usd(s.projected)}</span>
      </span>
    </div>
  );
}

/** What a night left undone, for its red-edged cell. */
function failed(r: Run): string {
  if (r.error) return r.error;
  const c = r.counts ?? {};
  const parts = [c.grading_failed ? `${c.grading_failed} grading` : "", c.analysis_failed ? `${c.analysis_failed} analysis` : ""].filter(Boolean);
  return parts.length ? `${parts.join(", ")} failed` : "";
}

export function RunsPage() {
  const quality = useQuery(qualityQuery);
  const runs = useQuery(runsQuery);
  const schedule = useQuery(scheduleQuery);
  const desktop = useDesktop();
  const cost = quality.data?.cost ?? [];
  const next = schedule.data?.jobs.find((j) => j.kind === "nightly") ?? schedule.data?.jobs[0];
  return (
    <>
      <PageHead title="Runs and cost" sub="Month to date per provider, the projection to the month's end, each night's run and the jobs outside it." />
      <Load q={quality} />
      {cost.map((s) => (
        <SpendRow key={s.provider} s={s} />
      ))}
      {quality.data && !cost.length && <div className="empty">No spend this month.</div>}
      {quality.data && <Later>Deep cards' month to date as a subscription estimate comes with the usage columns on card attempts.</Later>}
      {schedule.data && (
        <div className="nsub mt-3">
          {schedule.data.enabled ? (next ? `Next run ${dateTime(next.next)} (${schedule.data.zone}).` : "The schedule is on.") : "The schedule is off; runs start from the command line."}
        </div>
      )}
      <div className="cap">Nights</div>
      <Load q={runs} />
      {runs.data && (
        <div className={`mtbl runs ${desktop ? "c7" : "c4"}`}>
          <div className="mh">
            <span>Night</span>
            <span>Papers</span>
            {desktop && <span>Graded</span>}
            {desktop && <span>Digests</span>}
            {desktop && <span>Took</span>}
            <span>Cost</span>
            <span />
          </div>
          {runs.data.map((r) => {
            const bad = failed(r);
            return (
              <div className={`mr${bad ? " bad" : ""}`} key={r.id}>
                <span title={dateTime(r.started_at)}>
                  {rowDate(r.started_at)}
                  {r.kind !== "nightly" && <span className="faint">· {r.kind}</span>}
                </span>
                <span className="mono">{r.counts?.classified ?? 0}</span>
                {desktop && <span className="mono">{r.counts?.graded ?? 0}</span>}
                {desktop && <span className="mono faint">–</span>}
                {desktop && <span className="mono">{took(r.started_at, r.finished_at) || "running"}</span>}
                <span className="mono">{usd(r.cost)}</span>
                <span>{bad ? <span className="badt">{desktop ? bad : "failed"}</span> : ""}</span>
              </div>
            );
          })}
          {!runs.data.length && <div className="empty">No run yet.</div>}
        </div>
      )}
      <div className="caprow">
        <div className="cap">Jobs</div>
        <Link to="/settings/runs/jobs" className="inl">
          All jobs ›
        </Link>
      </div>
      <Later>Re-runs, replays, Deep cards asked for and digests written again come with the jobs API; nothing runs by itself.</Later>
    </>
  );
}

export function JobsPage() {
  return (
    <div className="setpg">
      <PageHead title="Jobs" sub="Work outside the nightly run: re-runs after an edit, replays, Deep cards asked for, digests written again. Each stops at its ceiling." deeper />
      <Later>The four groups (Not run, Running and queued, Failed, Done) come with the jobs API.</Later>
    </div>
  );
}
