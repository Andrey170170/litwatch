/** Runs and cost (DESIGN.md, "Runs and cost"): spend per provider against its budget, the schedule, the nights
 * as a table and the latest jobs; and Jobs, the work outside the nightly run in its four groups. */
import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { useJobAction } from "@/api/mutations";
import { jobsQuery, qualityQuery, runsQuery, scheduleQuery } from "@/api/queries";
import { useDesktop } from "@/hooks/useDesktop";
import { dateTime, plural, rowDate } from "@/lib/format";
import { ACTIONS, anyActive, GROUPS, type Job, jobKind, jobMeta, jobStatus, jobTone, latestJobs } from "@/lib/jobs";
import { digests, type Quality, runFailed, spendBar, type Spend, took, usd } from "@/lib/settings";
import { Failed, Load, PageHead, PaperLink } from "./parts";

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

/** Deep cards' month to date as a subscription estimate: no budget, never amber, "at least" while an attempt's
 * usage is unknown (DESIGN.md, "Runs and cost"). */
function DeepSpend({ d }: { d: NonNullable<Quality["deep"]> }) {
  return (
    <div className="spend deep">
      <span className="pn">Deep cards</span>
      <span className="sv2">
        {d.unknown ? "at least " : ""}
        {usd(d.list_cost)} at list prices · {plural(d.attempts, "attempt")}
        {d.unknown ? <span className="faint"> · {d.unknown} with usage unknown</span> : ""}
        <span className="faint"> · a subscription, not budgeted</span>
      </span>
    </div>
  );
}

export function RunsPage() {
  const quality = useQuery(qualityQuery("runs"));
  const runs = useQuery(runsQuery("runs"));
  const schedule = useQuery(scheduleQuery);
  const jobs = useQuery({ ...jobsQuery("runs"), refetchInterval: (q) => (anyActive(q.state.data) ? 10_000 : false) });
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
      {quality.data?.deep && <DeepSpend d={quality.data.deep} />}
      {schedule.data && (
        <div className="nsub mt-3">
          {schedule.data.enabled ? (next ? `Next run ${dateTime(next.next, schedule.data.zone)} (${schedule.data.zone}).` : "The schedule is on.") : "The schedule is off; runs start from the command line."}
        </div>
      )}
      <Failed q={schedule} what="the schedule" />
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
            const bad = runFailed(r);
            return (
              <div className={`mr${bad ? " bad" : ""}`} key={r.id}>
                <span title={dateTime(r.started_at)}>{rowDate(r.started_at)}</span>
                <span className="mono">{r.counts?.classified ?? 0}</span>
                {desktop && <span className="mono">{r.counts?.graded ?? 0}</span>}
                {desktop && (digests(r) === null ? <span className="mono faint">–</span> : <span className="mono">{digests(r)}</span>)}
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
      <Load q={jobs} />
      {jobs.data && latestJobs(jobs.data).map((j) => <JobRow key={j.key} job={j} zone={schedule.data?.zone} />)}
      {jobs.data && !latestJobs(jobs.data).length && <div className="empty">No job yet.</div>}
    </>
  );
}

/** A job, or a Deep card asked for: its kind and paper, where it stands, the line under it, a running one's bar,
 * and on the Jobs page (`full`) the actions its state allows. */
function JobRow({ job, zone, full }: { job: Job; zone?: string; full?: boolean }) {
  const act = useJobAction();
  const bar = job.state === "running" && job.papers > 1;
  return (
    <div className={`job ${jobTone(job.state)}`}>
      <div className="jh">
        <span className="jk">{jobKind(job)}</span>
        <span className="jw">{job.paper ? <PaperLink id={job.paper.id}>{job.paper.title}</PaperLink> : plural(job.papers, "paper")}</span>
        <span className="jst">{jobStatus(job.state)}</span>
      </div>
      <div className="nb">
        {jobMeta(job, zone)}
        {job.error && (job.state === "failed" || job.state === "stopped") && <span className="badt"> · {job.error}</span>}
      </div>
      {bar && (
        <span className="jbar" aria-hidden>
          <i style={{ width: `${(job.done / job.papers) * 100}%` }} />
        </span>
      )}
      {full && job.actions.length > 0 && (
        <div className="nact">
          {job.actions.map((a) => (
            <button key={a} className={`btn${a === "run" ? " pri" : ""}`} disabled={act.isPending} onClick={() => act.mutate({ job, action: a })}>
              {ACTIONS[a]}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

export function JobsPage() {
  const schedule = useQuery(scheduleQuery);
  // Polled while a job runs or waits its turn, so each moves on as the worker takes it.
  const jobs = useQuery({ ...jobsQuery("runs/jobs"), refetchInterval: (q) => (anyActive(q.state.data) ? 10_000 : false) });
  const shown = jobs.data ? GROUPS.filter(([g]) => jobs.data[g].length) : [];
  return (
    <div className="setpg">
      <PageHead title="Jobs" sub="Work outside the nightly run: papers processed now and Deep cards asked for." deeper />
      <Load q={jobs} />
      {shown.map(([g, label]) => (
        <div key={g}>
          <div className="cap">
            {label} · {jobs.data![g].length}
          </div>
          {jobs.data![g].map((j) => (
            <JobRow key={j.key} job={j} zone={schedule.data?.zone} full />
          ))}
        </div>
      ))}
      {jobs.data && !shown.length && <div className="empty">No job yet. Process on a paper outside the collection starts one.</div>}
    </div>
  );
}
