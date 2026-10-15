/** Overview (DESIGN.md, "Overview"): four tiles, then recall, precision, reactions and "Matters, no topic". */
import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { qualityQuery, runsQuery, sessionsQuery } from "@/api/queries";
import { Badge } from "@/components/Badge";
import { useDesktop } from "@/hooks/useDesktop";
import { rowDate } from "@/lib/format";
import { type ReactionReason, reasonLabel } from "@/lib/reactions";
import { type Quality, spendTotals, usd, waiting } from "@/lib/settings";
import { Later, Load, PageHead, PaperLink, Share, useTopicCards } from "./parts";

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

export function OverviewPage() {
  const quality = useQuery(qualityQuery);
  const runs = useQuery(runsQuery);
  const sessions = useQuery(sessionsQuery);
  const desktop = useDesktop();
  const q = quality.data;
  const night = runs.data?.find((r) => r.kind === "nightly") ?? runs.data?.[0];
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
  const { spent, budget } = spendTotals(q?.cost ?? []);
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
          <Tile to="runs" tone={night ? (night.error ? "fail" : "ok") : ""} big={night ? rowDate(night.started_at) : "–"}>
            {night ? `Last night · ${night.counts?.classified ?? 0} classified · ${night.counts?.graded ?? 0} graded · ${usd(night.cost)}` : "No run yet"}
          </Tile>
          <Tile to="runs" tone={over.length ? "warn" : "ok"} big={usd(spent)}>
            {budget ? `of ${usd(budget)} this month` : "this month"}
            {over.length ? ` · ${over.join(", ")} projected over` : ""}
          </Tile>
          <Tile to="calibrate" tone="" big={last ? rowDate(last.drawn_at) : "–"}>
            {last ? `Last Calibrate session · ${last.answered} of ${last.papers} answered` : "No Calibrate session yet"}
          </Tile>
        </div>
      )}
      {q && (
        <>
          <div className="cap">Recall per Focus topic</div>
          <div className="nsub">Over the papers labelled central or related, from the last replay. A replay starts from the command line until the jobs API lands.</div>
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
                  {name(r.topic)}
                  {[r.classification, r.membership, r.grading, r.end_to_end].map((s, i) => (
                    <span key={i}>{s.n ? <Share x={s.hits / s.n} /> : <span className="faint">–</span>}</span>
                  ))}
                  <span className="mono faint">{r.positives}</span>
                </div>
              ))}
            </div>
          ) : (
            <div className="empty">No replay has run yet.</div>
          )}
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
          <Later>Recall per cohort and "Run replay" come with the jobs API.</Later>
        </>
      )}
    </>
  );
}
