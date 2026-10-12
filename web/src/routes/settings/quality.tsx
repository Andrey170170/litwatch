import { useState } from "react";
import { Link, useNavigate } from "@tanstack/react-router";
import { useQuery, type UseQueryResult } from "@tanstack/react-query";
import type { TopicCard } from "@/api/client";
import { qualityQuery, sessionsQuery, useCorrect, useDismiss, useDraw, useGradeNow, useTriageByHand, type Awaiting, type Split } from "@/api/settings";
import { paperQuery, topicsQuery } from "@/api/queries";
import { LabelRow } from "@/components/LabelRow";
import { GRADE_WORD } from "@/components/paper-bits";
import { modelName } from "@/components/PaperView";
import { fmt } from "@/lib/format";
import { BTN, BTN_PRIMARY, Empty, FIELD, H2, Page, TABLE, pct } from "@/routes/settings/index";
import { MetricsView } from "@/routes/settings/topics";
import { cn } from "@/lib/utils";

/** Quality: what last night left at the top, then Calibrate and how the Watch topics are doing. */
export function QualityPage() {
  const q = useQuery(qualityQuery);
  const topics = useQuery(topicsQuery);
  const names = Object.fromEntries((topics.data ?? []).map((t) => [t.slug, t.name]));
  const name = (slug: string | null) => (slug ? (names[slug] ?? slug) : "no topic");
  if (q.isPending) return <Page title="Quality">{null}</Page>;
  if (q.isError || !q.data) return <Page title="Quality"><p className="text-destructive">Could not load.</p></Page>;
  const d = q.data;
  const n = d.night;
  const quiet = !n.error && n.splits.length === 0 && n.ungraded.length === 0 && n.awaiting_triage.length === 0;

  return (
    <Page title="Quality" sub="Last night first, then how well the Watch topics are doing. Nothing here is required reading.">
      <H2 note={n.run ? `run ${n.run}` : undefined}>Last night</H2>
      {n.error && (
        <div className="mb-3 rounded-lg border border-destructive/40 bg-destructive/5 px-3.5 py-2.5">
          <div className="font-medium text-destructive">The run failed.</div>
          <pre className="mt-1 whitespace-pre-wrap font-mono text-[12px] text-body">{n.error}</pre>
          <Link to="/settings/runs" className="mt-1 inline-block text-[12.5px] underline decoration-border">
            Runs and cost
          </Link>
        </div>
      )}
      {quiet && <Empty>All quiet: no disagreements, nothing ungraded, nothing awaiting triage.</Empty>}

      {n.splits.length > 0 && (
        <>
          <h3 className="mt-4 mb-1 text-[13px] font-medium">
            {n.splits.length} {n.splits.length === 1 ? "disagreement" : "disagreements"}
            <span className="ml-2 font-normal text-muted-foreground">the two graders disagreed; the more inclusive grade stood. Your call settles it.</span>
          </h3>
          <div className="divide-y divide-border">
            {n.splits.map((s) => (
              <SplitView key={`${s.paper_id}-${s.topic}`} s={s} topicName={name(s.topic)} />
            ))}
          </div>
        </>
      )}

      {n.ungraded.length > 0 && (
        <>
          <h3 className="mt-5 mb-1 text-[13px] font-medium">
            {n.ungraded.length} ungraded <span className="ml-2 font-normal text-muted-foreground">grading failed three nights running</span>
          </h3>
          <table className={TABLE}>
            <tbody>
              {n.ungraded.map((u) => (
                <UngradedRow key={`${u.paper_id}-${u.topic}`} u={u} topicName={name(u.topic)} />
              ))}
            </tbody>
          </table>
        </>
      )}

      {n.awaiting_triage.length > 0 && (
        <>
          <h3 className="mt-5 mb-1 text-[13px] font-medium">
            {n.awaiting_triage.length} awaiting triage{" "}
            <span className="ml-2 font-normal text-muted-foreground">triage stopped after three failed nights; dismiss it, triage it yourself, or fix the text it choked on</span>
          </h3>
          <div className="divide-y divide-border">
            {n.awaiting_triage.map((a) => (
              <AwaitingView key={a.paper_id} a={a} topics={topics} names={names} />
            ))}
          </div>
        </>
      )}

      <CalibrateCard />

      <H2 note="from Calibrate draws over the last 90 days; the verdict at the draw, not a later re-grade">Precision</H2>
      {d.calibration.length === 0 ? (
        <Empty>No Calibrate answers yet. A session of about twenty papers starts the number.</Empty>
      ) : (
        <table className={TABLE}>
          <thead>
            <tr>
              <th>Topic</th>
              <th className="text-right">Precision at relevant</th>
              <th className="text-right">Dropped but wanted</th>
            </tr>
          </thead>
          <tbody>
            {d.calibration.map((c) => (
              <tr key={c.topic}>
                <td>{name(c.topic)}</td>
                <td className="tabular text-right">
                  {pct(c.precision)} <span className="text-muted-foreground">of {c.precision_n ?? 0}</span>
                </td>
                <td className="tabular text-right">
                  {pct(c.dropped)} <span className="text-muted-foreground">of {c.dropped_n ?? 0}</span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <H2 note={d.evaluation ? `replay ${d.evaluation.eval_run}${d.evaluation.note ? `, ${d.evaluation.note}` : ""}` : undefined}>Recall</H2>
      {!d.evaluation ? (
        <Empty>No evaluation replay yet.</Empty>
      ) : (
        Object.entries(d.evaluation.topics).map(([slug, m]) => (
          <div key={slug} className="mb-3">
            <div className="font-medium">{name(slug)}</div>
            <MetricsView m={m} evaluation={d.evaluation!} />
          </div>
        ))
      )}

      <H2 note="papers shown in issues over 30 days that got a thumb down for not for me, unrelated or overrated">Correction rate</H2>
      {d.correction_rate.length === 0 ? (
        <Empty>Nothing shown yet.</Empty>
      ) : (
        <table className={TABLE}>
          <thead>
            <tr>
              <th>Topic</th>
              <th className="text-right">Shown</th>
              <th className="text-right">Corrected</th>
              <th className="text-right">Rate</th>
            </tr>
          </thead>
          <tbody>
            {d.correction_rate.map((c) => (
              <tr key={c.topic}>
                <td>{name(c.topic)}</td>
                <td className="tabular text-right">{c.shown}</td>
                <td className="tabular text-right">{c.corrected}</td>
                <td className="tabular text-right">{c.shown ? pct(c.corrected / c.shown) : "–"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <H2 note="papers you said matter that fit no Watch topic: a missing topic, or a rubric drawn too narrowly">Matters, no topic</H2>
      {d.matters_no_topic.length === 0 ? (
        <Empty>None.</Empty>
      ) : (
        <ul className="divide-y divide-border">
          {d.matters_no_topic.map((m) => (
            <li key={m.paper_id} className="py-2">
              <Link to="/paper/$" params={{ _splat: m.paper_id }} className="font-medium hover:underline">
                {m.title}
              </Link>
              <div className="text-[12.5px] text-muted-foreground">{fmt.published(m.at)}</div>
              {m.note && <div className="mt-0.5 text-body">{m.note}</div>}
            </li>
          ))}
        </ul>
      )}

      <H2 note="every thumb, with its reason; no automatic effect">Collected reactions</H2>
      {d.collected.length === 0 ? (
        <Empty>No reactions yet.</Empty>
      ) : (
        <table className={TABLE}>
          <thead>
            <tr>
              <th>Paper</th>
              <th>Topic</th>
              <th>Reaction</th>
              <th>When</th>
            </tr>
          </thead>
          <tbody>
            {d.collected.map((r, i) => (
              <tr key={i}>
                <td>
                  <Link to="/paper/$" params={{ _splat: r.paper_id }} className="hover:underline">
                    {r.title}
                  </Link>
                </td>
                <td className="whitespace-nowrap">{name(r.topic)}</td>
                <td className="whitespace-nowrap">
                  {r.value === "up" ? "👍" : "👎"} {r.reason ? REASON_WORD[r.reason] ?? r.reason : ""}
                </td>
                <td className="whitespace-nowrap text-muted-foreground">{fmt.cell(r.reacted_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Page>
  );
}

const REASON_WORD: Record<string, string> = {
  not_for_me: "not for me",
  unrelated: "unrelated to the topic",
  already_knew: "already knew it",
  overrated: "overrated",
  great_find: "great find",
};

function SplitView({ s, topicName }: { s: Split; topicName: string }) {
  return (
    <div className="py-3">
      <div className="flex flex-wrap items-baseline gap-x-3">
        <Link to="/paper/$" params={{ _splat: s.paper_id }} search={{ topic: s.topic }} className="font-serif text-[15.5px] font-semibold hover:underline">
          {s.title}
        </Link>
        <span className="text-[12.5px] text-muted-foreground">
          {topicName} · shown as {GRADE_WORD[s.grade]}
        </span>
      </div>
      <div className="mt-1.5 grid gap-x-4 gap-y-1.5 desk:grid-cols-2">
        {([["judge", s.judge], ["check", s.check]] as const).map(([role, o]) =>
          o ? (
            <div key={role} className="text-[13px] text-body">
              <small className="block text-muted-foreground">
                {modelName(o.model)}, {role}
              </small>
              <b>{GRADE_WORD[o.grade]}.</b> {o.reason}
            </div>
          ) : null,
        )}
      </div>
      <div className="mt-2">
        <LabelRow paperId={s.paper_id} topic={s.topic} current={s.label?.value} />
      </div>
    </div>
  );
}

function UngradedRow({ u, topicName }: { u: { paper_id: string; topic: string; failed_runs: number; last_error: string | null; title: string }; topicName: string }) {
  const grade = useGradeNow();
  return (
    <tr>
      <td>
        <Link to="/paper/$" params={{ _splat: u.paper_id }} className="font-medium hover:underline">
          {u.title}
        </Link>
        <div className="text-[12.5px] text-muted-foreground">
          {topicName} · {u.failed_runs} failed {u.failed_runs === 1 ? "night" : "nights"}
        </div>
        {u.last_error && <div className="font-mono text-[11.5px] text-muted-foreground">{u.last_error}</div>}
      </td>
      <td className="text-right whitespace-nowrap">
        <button type="button" className={BTN} disabled={grade.isPending} onClick={() => grade.mutate({ paper_id: u.paper_id, topic: u.topic })}>
          {grade.isPending ? "Grading…" : "Grade it now"}
        </button>
      </td>
    </tr>
  );
}

/** A paper triage kept failing on, with its three ways out (spec decision 24). */
function AwaitingView({ a, topics, names }: { a: Awaiting; topics: UseQueryResult<TopicCard[]>; names: Record<string, string> }) {
  const [open, setOpen] = useState<"triage" | "fix" | null>(null);
  const [arming, setArming] = useState(false);
  const dismiss = useDismiss();
  const toggle = (which: "triage" | "fix") => setOpen((o) => (o === which ? null : which));
  return (
    <div className="py-3">
      <div className="flex flex-wrap items-start justify-between gap-x-4 gap-y-2">
        <div className="min-w-0">
          <Link to="/paper/$" params={{ _splat: a.paper_id }} className="font-medium hover:underline">
            {a.title}
          </Link>
          <div className="text-[12.5px] text-muted-foreground">
            {a.failed_runs} failed {a.failed_runs === 1 ? "night" : "nights"}
          </div>
          {a.last_error && <div className="font-mono text-[11.5px] text-muted-foreground">{a.last_error}</div>}
        </div>
        <div className="flex flex-wrap gap-1.5">
          <button type="button" className={cn(BTN, open === "triage" && "border-foreground")} aria-expanded={open === "triage"} onClick={() => toggle("triage")}>
            Triage by hand
          </button>
          <button type="button" className={cn(BTN, open === "fix" && "border-foreground")} aria-expanded={open === "fix"} onClick={() => toggle("fix")}>
            Fix the text
          </button>
          <button
            type="button"
            className={cn(BTN, arming && "border-destructive text-destructive")}
            disabled={dismiss.isPending}
            onBlur={() => setArming(false)}
            onClick={() => (arming ? dismiss.mutate(a.paper_id) : setArming(true))}
          >
            {dismiss.isPending ? "Dismissing…" : arming ? "Dismiss, never triage" : "Dismiss"}
          </button>
        </div>
      </div>
      {open === "triage" && <TriageByHand paperId={a.paper_id} topics={topics} names={names} />}
      {open === "fix" && <FixText paperId={a.paper_id} />}
    </div>
  );
}

/**
 * Triage's answers, given by the owner: a chip per active topic, off unless pressed. Nothing
 * is sent until the topics are known: an empty answer would drop the paper as belonging nowhere.
 */
function TriageByHand({ paperId, topics, names }: { paperId: string; topics: UseQueryResult<TopicCard[]>; names: Record<string, string> }) {
  const [yes, setYes] = useState<Set<string>>(new Set());
  const send = useTriageByHand(names);
  const active = (topics.data ?? []).filter((t) => t.status === "active");
  const watch = active.filter((t) => t.kind === "watch");
  const fields = active.filter((t) => t.kind !== "watch");
  const flip = (slug: string) =>
    setYes((s) => {
      const n = new Set(s);
      if (n.has(slug)) n.delete(slug);
      else n.add(slug);
      return n;
    });
  const group = (label: string, list: TopicCard[]) =>
    list.length > 0 && (
      <div role="group" aria-label={label} className="mt-2">
        <p className="mb-1 text-[12.5px] text-muted-foreground">{label}</p>
        <div className="flex flex-wrap gap-1.5">
          {list.map((t) => (
            <button
              key={t.slug}
              type="button"
              aria-pressed={yes.has(t.slug)}
              onClick={() => flip(t.slug)}
              className={cn("min-h-[36px] rounded-full border border-border px-3 text-[13px]", yes.has(t.slug) ? "border-foreground bg-foreground text-background" : "text-muted-foreground hover:text-foreground")}
            >
              {t.name}
            </button>
          ))}
        </div>
      </div>
    );
  return (
    <div className="mt-3 rounded-lg border border-border px-3.5 py-3">
      {topics.isPending && <p className="text-[12.5px] text-muted-foreground">Loading the topics…</p>}
      {topics.isError && (
        <p className="text-[13px] text-destructive">
          The topics could not be loaded, so nothing can be sent.{" "}
          <button type="button" className="underline decoration-destructive/50" onClick={() => void topics.refetch()}>
            Try again
          </button>
        </p>
      )}
      {group("Could it belong to a Watch topic?", watch)}
      {group("Is it in a field?", fields)}
      <div className="mt-3 flex flex-wrap items-center gap-2">
        <button
          type="button"
          className={BTN_PRIMARY}
          disabled={send.isPending || !topics.isSuccess}
          onClick={() => send.mutate({ paperId, answers: Object.fromEntries(active.map((t) => [t.slug, yes.has(t.slug)])) })}
        >
          {send.isPending ? "Sending…" : "Send it on"}
        </button>
        <span className="text-[12.5px] text-muted-foreground">
          {yes.size === 0 ? "With nothing pressed, it belongs nowhere and is left alone." : "The keyword audit and grading follow on the next run."}
        </span>
      </div>
    </div>
  );
}

/** The title and abstract as stored, to correct; the fix counts as a rewrite and triage runs again. */
function FixText({ paperId }: { paperId: string }) {
  const paper = useQuery(paperQuery(paperId));
  const correct = useCorrect();
  const [title, setTitle] = useState<string | null>(null);
  const [abstract, setAbstract] = useState<string | null>(null);
  if (paper.isPending) return <p className="mt-2 text-[12.5px] text-muted-foreground">Opening the text…</p>;
  if (!paper.data) return <p className="mt-2 text-[12.5px] text-destructive">The paper could not be loaded.</p>;
  const t = title ?? paper.data.title;
  const ab = abstract ?? paper.data.abstract;
  const changed = (t.trim() !== paper.data.title && t.trim() !== "") || (ab.trim() !== paper.data.abstract && ab.trim() !== "");
  return (
    <div className="mt-3 grid gap-3 rounded-lg border border-border px-3.5 py-3">
      <label className="block">
        <span className="mb-1 block text-[12.5px] font-medium">Title</span>
        <input className={FIELD} value={t} onChange={(e) => setTitle(e.target.value)} />
      </label>
      <label className="block">
        <span className="mb-1 block text-[12.5px] font-medium">Abstract</span>
        <textarea className={cn(FIELD, "min-h-[140px]")} value={ab} onChange={(e) => setAbstract(e.target.value)} />
      </label>
      <div className="flex flex-wrap items-center gap-2">
        <button
          type="button"
          className={BTN_PRIMARY}
          disabled={!changed || correct.isPending}
          onClick={() =>
            correct.mutate({
              paperId,
              title: t.trim() !== paper.data.title ? t.trim() : null,
              abstract: ab.trim() !== paper.data.abstract ? ab.trim() : null,
            })
          }
        >
          {correct.isPending ? "Saving…" : "Save and triage again"}
        </button>
        <span className="text-[12.5px] text-muted-foreground">Kept for every model call until arXiv publishes a newer version.</span>
      </div>
    </div>
  );
}

/** Calibrate: start a session or continue one. */
function CalibrateCard() {
  const sessions = useQuery(sessionsQuery);
  const draw = useDraw();
  const navigate = useNavigate();
  const open = (sessions.data ?? []).filter((s) => s.answered < s.papers);
  return (
    <div className="mt-7 rounded-xl border border-foreground px-4 py-3.5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="font-serif text-[18px] font-semibold">Calibrate</h2>
          <p className="mt-0.5 max-w-[560px] text-[13px] text-muted-foreground">
            About twenty papers, one per screen, drawn from what each Watch topic showed and dropped in the last month. You say where each one fits, whether it matters to you, and whether its take is right. Answers save as you give them.
          </p>
        </div>
        <button
          type="button"
          className={BTN_PRIMARY}
          disabled={draw.isPending}
          onClick={() => draw.mutate(undefined, { onSuccess: (r) => void navigate({ to: "/settings/calibrate/$id", params: { id: String(r.id) }, search: {} }) })}
        >
          {draw.isPending ? "Drawing…" : "Start a session"}
        </button>
      </div>
      {(sessions.data ?? []).length > 0 && (
        <table className={`${TABLE} mt-3`}>
          <tbody>
            {(sessions.data ?? []).slice(0, 6).map((s) => (
              <tr key={s.id}>
                <td>
                  <Link to="/settings/calibrate/$id" params={{ id: String(s.id) }} search={{}} className="hover:underline">
                    Session {s.id}
                  </Link>
                  <span className="ml-2 text-muted-foreground">{fmt.cell(s.drawn_at)}</span>
                </td>
                <td className="tabular text-right whitespace-nowrap">
                  {s.answered} of {s.papers} answered
                </td>
                <td className="text-right">
                  <Link to="/settings/calibrate/$id" params={{ id: String(s.id) }} search={{}} className={BTN}>
                    {open.some((o) => o.id === s.id) ? "Continue" : "Open"}
                  </Link>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
