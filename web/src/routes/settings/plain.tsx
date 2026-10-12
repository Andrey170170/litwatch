import { Link } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import { configQuery, missesQuery, runsQuery, scheduleQuery, useRetrace, type Miss } from "@/api/settings";
import { topicsQuery } from "@/api/queries";
import { GRADE_WORD } from "@/components/paper-bits";
import { modelName } from "@/components/PaperView";
import { fmt } from "@/lib/format";
import { fireWords } from "@/lib/schedule";
import { BTN, Empty, Page, TABLE, money } from "@/routes/settings/index";

/** Misses: each report with what triage, the keyword audit and both graders said. */
export function MissesPage() {
  const misses = useQuery(missesQuery);
  const topics = useQuery(topicsQuery);
  const names = Object.fromEntries((topics.data ?? []).map((t) => [t.slug, t.name]));
  return (
    <Page title="Misses" sub="Papers you found elsewhere that litwatch did not surface, and what each stage said about them when asked again.">
      {misses.data?.length === 0 && <Empty>No misses reported. "Should have been found for a topic…" on a Papers row files one.</Empty>}
      <div className="divide-y divide-border">
        {(misses.data ?? []).map((m) => (
          <MissView key={m.id} m={m} topicName={names[m.topic] ?? m.topic} />
        ))}
      </div>
    </Page>
  );
}

function MissView({ m, topicName }: { m: Miss; topicName: string }) {
  const retrace = useRetrace();
  const failed = m.trace === "failed";
  const pending = m.trace !== "done" && !failed;
  return (
    <div className="py-3.5">
      <div className="flex flex-wrap items-baseline gap-x-3">
        <Link to="/paper/$" params={{ _splat: m.paper_id }} search={{ topic: m.topic }} className="font-serif text-[15.5px] font-semibold hover:underline">
          {m.title}
        </Link>
        <span className="text-[12.5px] text-muted-foreground">
          {topicName} · reported {fmt.cell(m.reported_at)}
        </span>
      </div>
      {pending && <p className="mt-1 text-[13px] text-muted-foreground">Tracing what each stage says…</p>}
      {failed && (
        <div className="mt-2 rounded-lg border border-destructive/40 bg-destructive/5 px-3 py-2">
          <div className="text-[13px] font-medium text-destructive">The trace failed.</div>
          {m.trace_error && <pre className="mt-1 whitespace-pre-wrap font-mono text-[12px] text-body">{m.trace_error}</pre>}
          <button type="button" className={`${BTN} mt-2`} disabled={retrace.isPending} onClick={() => retrace.mutate(m.id)}>
            Trace again
          </button>
        </div>
      )}
      {m.trace === "done" && (
        <div className="mt-2 grid gap-x-4 gap-y-2 text-[13px] desk:grid-cols-2">
          <Stage label="Triage">
            {m.triage_yes === null ? "did not answer" : m.triage_yes ? "candidate" : "not a candidate"}
            {m.triage_why && <span className="text-muted-foreground">: “{m.triage_why}”</span>}
          </Stage>
          <Stage label="Keyword audit">{m.keyword === null ? "–" : m.keyword ? "a pattern matched" : "no pattern matched"}</Stage>
          {m.judge && (
            <Stage label={`${modelName(m.judge.model)}, judge`}>
              <b>{GRADE_WORD[m.judge.grade]}.</b> {m.judge.reason}
            </Stage>
          )}
          {m.check && (
            <Stage label={`${modelName(m.check.model)}, check`}>
              <b>{GRADE_WORD[m.check.grade]}.</b> {m.check.reason}
            </Stage>
          )}
          <Stage label="Would have been">{m.grade ? GRADE_WORD[m.grade] : m.grade_error ? `not graded: ${m.grade_error}` : "not graded"}</Stage>
          {m.error && (
            <Stage label="Error">
              <span className="font-mono text-[12px]">{m.error}</span>
            </Stage>
          )}
        </div>
      )}
    </div>
  );
}

function Stage({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="text-body">
      <small className="block text-muted-foreground">{label}</small>
      {children}
    </div>
  );
}

/** The schedule in one line: on, with when each job next fires; or off, with when each would. */
export function ScheduleLine() {
  const schedule = useQuery(scheduleQuery);
  const s = schedule.data;
  if (!s) return null;
  const fires = s.jobs.map((j) => (
    <span key={j.kind} title={j.cron}>
      {j.kind} {fireWords(j.next, s.zone)}
    </span>
  ));
  const list = fires.flatMap((f, i) => (i === 0 ? [f] : [i === fires.length - 1 ? " and " : ", ", f]));
  return (
    <p className="mb-4 rounded-lg border border-border px-3.5 py-2.5 text-[13px]">
      {s.enabled ? (
        <>
          <b className="font-medium">On a schedule:</b> {list} <span className="text-muted-foreground">({s.zone}).</span>
        </>
      ) : (
        <>
          <b className="font-medium">No schedule:</b> runs are started by hand until cutover.{" "}
          <span className="text-muted-foreground">
            Once enabled, the next would be {list} ({s.zone}).
          </span>
        </>
      )}
    </p>
  );
}

/** Runs and cost: the schedule, then a row per run. */
export function RunsPage() {
  const runs = useQuery(runsQuery);
  return (
    <Page title="Runs and cost" sub="Every run, newest first: what it did and what it cost.">
      <ScheduleLine />
      {runs.data?.length === 0 && <Empty>No runs yet.</Empty>}
      {runs.data && runs.data.length > 0 && (
        <div className="overflow-x-auto">
          <table className={TABLE}>
            <thead>
              <tr>
                <th>Run</th>
                <th>Kind</th>
                <th>Started</th>
                <th className="text-right">Took</th>
                <th className="text-right">Cost</th>
                <th>Counts</th>
                <th>Error</th>
              </tr>
            </thead>
            <tbody>
              {runs.data.map((r) => (
                <tr key={r.id}>
                  <td className="tabular text-muted-foreground">{r.id}</td>
                  <td className="whitespace-nowrap">{r.kind}</td>
                  <td className="whitespace-nowrap">
                    {fmt.cell(r.started_at)} {fmt.time(r.started_at)}
                  </td>
                  <td className="tabular text-right whitespace-nowrap">{took(r.started_at, r.finished_at)}</td>
                  <td className="tabular text-right">{money(r.cost)}</td>
                  <td className="text-[12px] text-muted-foreground">{countsLine(r.counts, r.fetched)}</td>
                  <td className="max-w-[280px] font-mono text-[11.5px] text-destructive">{r.error}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Page>
  );
}

function took(a: string, b: string | null): string {
  if (!b) return "running";
  const s = Math.round((new Date(b).getTime() - new Date(a).getTime()) / 1000);
  return s < 90 ? `${s}s` : `${Math.round(s / 60)} min`;
}

const COUNT_ORDER = ["triaged", "candidates", "graded", "split", "takes", "issue_entries", "reviews", "reviews_failed", "rewritten", "triage_failed", "grading_failed"];
const ONE: Record<string, string> = { candidates: "candidate", takes: "take", issue_entries: "issue entry", reviews: "review", reviews_failed: "review failed" };

function countsLine(counts: Record<string, unknown> | null, fetched: number | null): string {
  const parts: string[] = [];
  if (fetched) parts.push(`${fetched} fetched`);
  for (const k of COUNT_ORDER) {
    const v = counts?.[k];
    if (typeof v === "number" && v > 0) parts.push(`${v} ${v === 1 && ONE[k] ? ONE[k] : k.replaceAll("_", " ")}`);
  }
  if (!parts.length && counts && typeof counts.topic === "string") parts.push(String(counts.topic));
  return parts.join(" · ");
}

/** Configuration: the loaded litwatch.toml, read-only. */
export function ConfigPage() {
  const config = useQuery(configQuery);
  return (
    <Page title="Configuration" sub="What litwatch loaded from litwatch.toml, secrets masked. The file is the source of truth; there is nothing to change here.">
      {config.data && <pre className="overflow-x-auto rounded-lg border border-border bg-muted px-3.5 py-3 font-mono text-[12px] leading-[1.5]">{JSON.stringify(config.data, null, 2)}</pre>}
    </Page>
  );
}

export function IntegrationsPage() {
  return (
    <Page title="Integrations" sub="One card per module, with its outbox under them.">
      <Empty>None are built yet. Zotero, the archive, deep cards, Nextcloud and the webhook arrive with M6.</Empty>
    </Page>
  );
}
