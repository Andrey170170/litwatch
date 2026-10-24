/** Overview (DESIGN.md, "Overview"): four tiles, then recall, precision, reactions and "Matters, no topic". */
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { useEffect, useRef } from "react";
import { qualityQuery, replayOfferQuery, runsQuery, sessionsQuery } from "@/api/queries";
import { Badge } from "@/components/Badge";
import { useDesktop } from "@/hooks/useDesktop";
import { rowDate } from "@/lib/format";
import { type ReactionReason, reasonLabel } from "@/lib/reactions";
import { digests, type Quality, runFailed, spendTotals, usd, waiting } from "@/lib/settings";
import { Failed, Load, PageHead, PaperLink, Share, useTopicCards } from "./parts";
import { RunReplay } from "./replay";

type Tone = "ask" | "fail" | "warn" | "ok" | "";

function Tile({ to, tone, big, children }: { to: "needs" | "runs" | "calibrate"; tone: Tone; big: string; children: React.ReactNode }) {
  return (
    <Link to="/settings/$page" params={{ page: to }} className={`tile ${tone}`}>
      <span className="big">{big}</span>
      <span className="tc">{children}</span>
    </Link>
  );
}

/** The reasons as columns: the ones that point at classification first, then grading, then the rest. */
const COLUMNS: ReactionReason[] = ["wrong_topic", "overrated", "underrated", "not_interesting", "already_knew", "great_find"];

function Reactions({ q, name }: { q: Quality; name: (slug: string) => React.ReactNode }) {
  const topics = [...new Set(q.reactions.map((r) => r.topic))];
  if (!topics.length) return <div className="empty">No reactions in the last 30 days.</div>;
  const n = (t: string, reason: ReactionReason) => q.reactions.find((r) => r.topic === t && r.reason === reason)?.n ?? 0;
  const points = (reason: ReactionReason, x: number) => (x >= 3 && (reason === "wrong_topic" || reason === "overrated" || reason === "underrated") ? "warnt" : x ? "" : "faint");
  return (
    <div className="mtbl r7">
      <div className="mh">
        <span />
        {COLUMNS.map((c) => (
          <span key={c}>{reasonLabel(c)}</span>
        ))}
      </div>
      {topics.map((t) => (
        <div className="mr" key={t}>
          {name(t)}
          {COLUMNS.map((c) => (
            <span key={c} className={`mono ${points(c, n(t, c))}`}>
              {n(t, c)}
            </span>
          ))}
        </div>
      ))}
    </div>
  );
}

/** While a replay is queued or running, its offer and the recall it feeds are read every `REPLAY_POLL` ms; once it
 * has ended, recall is read once more, so the last numbers come after its end. */
const REPLAY_POLL = 10_000;

export function OverviewPage() {
  const offer = useQuery({ ...replayOfferQuery, refetchInterval: (q) => (q.state.data?.running ? REPLAY_POLL : false) });
  const replaying = !!offer.data?.running;
  const quality = useQuery({ ...qualityQuery("overview"), refetchInterval: replaying ? REPLAY_POLL : false });
  const qc = useQueryClient();
  const was = useRef(replaying);
  useEffect(() => {
    // a read begun before the end would be joined, and is old: it is dropped for a new one
    const key = qualityQuery("overview").queryKey;
    if (was.current && !replaying) void qc.cancelQueries({ queryKey: key, exact: true }).then(() => qc.invalidateQueries({ queryKey: key, exact: true }));
    was.current = replaying;
  }, [replaying, qc]);
  const runs = useQuery(runsQuery("overview"));
  const sessions = useQuery(sessionsQuery("overview"));
  const desktop = useDesktop();
  const q = quality.data;
  // Last night is the run the quality read names; its counts come from the runs list, which may no longer hold it.
  const night = q?.night.run != null ? runs.data?.find((r) => r.id === q.night.run) : undefined;
  // Red as Runs and cost marks it: the run failed, or some of its papers' steps or digests did.
  const nightFailed = !!q?.night.error || (night ? !!runFailed(night) : false);
  const last = sessions.data?.[0];
  const cards = useTopicCards();
  const name = (slug: string) => {
    const t = cards[slug];
    return (
      <span className="tn">
        {t ? <Badge topic={t} size="sm" /> : null}
        {desktop || !t ? <span>{t?.name ?? slug}</span> : null}
      </span>
    );
  };
  const open = q ? waiting(q) : 0;
  const { spent, budget, unbudgeted } = spendTotals(q?.cost ?? []);
  const over = q?.cost.filter((c) => c.over).map((c) => c.provider) ?? [];
  const replay = q?.recall;
  return (
    <>
      <PageHead title="Overview" sub={`How the pipeline is doing.${replay ? ` Recall from the replay of ${rowDate(replay.started_at)};` : " Recall waits for a replay;"} precision from Calibrate over 90 days.`} />
      <Load q={quality} />
      {q && (
        <div className="tiles">
          <Tile to="needs" tone={open ? "ask" : "ok"} big={open ? String(open) : "✓"}>
            {open ? `${open === 1 ? "thing needs" : "things need"} you` : "Nothing needs you"}
          </Tile>
          <Tile to="runs" tone={q?.night.run == null ? "" : nightFailed ? "fail" : "ok"} big={night ? rowDate(night.started_at) : "–"}>
            {night
              ? `Last night · ${night.counts?.classified ?? 0} classified · ${night.counts?.graded ?? 0} graded${digests(night) === null ? "" : ` · ${digests(night)} digests`} · ${usd(night.cost)}`
              : q?.night.run == null
                ? "No night yet"
                : runs.data
                  ? "Last night · no longer among the runs listed"
                  : ""}
          </Tile>
          <Tile to="runs" tone={over.length ? "warn" : "ok"} big={usd(spent)}>
            {budget ? `of ${usd(budget)} this month` : "this month"}
            {unbudgeted ? ` · ${usd(unbudgeted)} more without a budget` : ""}
            {over.length ? ` · ${over.join(", ")} projected over` : ""}
          </Tile>
          <Tile to="calibrate" tone="" big={last ? rowDate(last.drawn_at) : "–"}>
            {last ? `Last Calibrate session · ${last.answered} of ${last.papers} answered` : sessions.data ? "No Calibrate session yet" : ""}
          </Tile>
        </div>
      )}
      <Failed q={runs} what="the runs" />
      <Failed q={sessions} what="the Calibrate sessions" />
      {q && (
        <>
          <div className="cap">Recall per Focus topic</div>
          <div className="nsub">Over the papers labelled central or related, per cohort, from the last replay.</div>
          {replay?.recall.length ? (
            <div className="mtbl r5">
              <div className="mh">
                <span />
                <span>Classification</span>
                <span>Membership</span>
                <span>Grading</span>
                <span>End to end</span>
                <span>n</span>
              </div>
              {replay.recall.map((r) => (
                <div className="mr" key={`${r.cohort}:${r.topic}`}>
                  <span>
                    {name(r.topic)} <span className="faint">· {r.cohort}</span>
                  </span>
                  {[r.classification, r.membership, r.grading, r.end_to_end].map((s, i) => (
                    <span key={i}>{s.n ? <Share x={s.hits / s.n} /> : <span className="faint">–</span>}</span>
                  ))}
                  <span className="mono faint">{r.positives}</span>
                </div>
              ))}
            </div>
          ) : (
            <div className="empty">{replay ? "This replay has no outcomes yet." : "No replay has run yet."}</div>
          )}
          <RunReplay offer={offer} />
          <div className="cap">Precision at relevant</div>
          <div className="nsub">
            From Calibrate's draws over the last 90 days.{" "}
            <Link to="/settings/$page" params={{ page: "calibrate" }} className="inl">
              Calibrate
            </Link>
          </div>
          {q.calibration.length ? (
            <div className="mtbl r3">
              <div className="mh">
                <span />
                <span>Relevant or above, owner agrees</span>
                <span>Below, owner wanted it</span>
              </div>
              {q.calibration.map((p) => (
                <div className="mr" key={p.topic}>
                  {name(p.topic)}
                  <span>
                    {p.precision == null ? <span className="faint">–</span> : <Share x={p.precision} />} <span className="faint mono">of {p.precision_n}</span>
                  </span>
                  <span>
                    {p.dropped == null ? <span className="faint">–</span> : <Share x={p.dropped} hi={false} good={0.05} ok={0.1} />} <span className="faint mono">of {p.dropped_n}</span>
                  </span>
                </div>
              ))}
            </div>
          ) : (
            <div className="empty">No Calibrate answers in the last 90 days.</div>
          )}
          <div className="cap">Reactions, last 30 days</div>
          <div className="nsub">Wrong topic points at classification, over- and underrated at grading.</div>
          <Reactions q={q} name={name} />
          <div className="cap">Matters, no topic</div>
          <div className="nsub">Papers marked as mattering that no followed topic holds: a missing topic, or a class too narrow.</div>
          {q.matters_no_topic.length ? (
            <div className="list">
              {q.matters_no_topic.map((m) => (
                <PaperLink key={m.paper_id} id={m.paper_id} className="prow nt-row">
                  <span className="t">
                    {m.title}
                    {m.note && <span className="g">{m.note}</span>}
                  </span>
                  <span className="dt">{rowDate(m.at)}</span>
                </PaperLink>
              ))}
            </div>
          ) : (
            <div className="empty">None.</div>
          )}
        </>
      )}
    </>
  );
}
