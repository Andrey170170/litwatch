/**
 * Calibrate (DESIGN.md, "Calibrate"): a first page with "Start a session" and the past sessions; a session's screen,
 * one paper at a time, blind, with the strip and the questions; a session done or opened later shows its review,
 * the owner's Fit against the pipeline's draw, amber where they differ.
 */
import { useQuery } from "@tanstack/react-query";
import { Navigate, useNavigate, useParams, useSearch } from "@tanstack/react-router";
import { useEffect, useState } from "react";
import type { Schemas } from "@/api/client";
import { useAnswer, useDrawSession, useMore } from "@/api/mutations";
import { paperQuery, sessionQuery, sessionsQuery, taxonomyQuery, topicsQuery } from "@/api/queries";
import { Badge } from "@/components/Badge";
import { Sheet } from "@/components/Sheet";
import { Fill } from "@/components/slots";
import { useDesktop } from "@/hooks/useDesktop";
import { authorsShort, rowDate } from "@/lib/format";
import { differs, type LabelValue, pipelineSide, type Session, type SessionItem } from "@/lib/settings";
import { FitSeg, Later, Load, PageHead, PaperLink } from "./parts";

type Value = NonNullable<Schemas["Answer"]["value"]>;
const VALUES: { v: Value; name: string }[] = [
  { v: "matters", name: "matters" },
  { v: "fine", name: "fine" },
  { v: "not_for_me", name: "not for me" },
];

export function CalibratePage() {
  const sessions = useQuery(sessionsQuery);
  const draw = useDrawSession();
  const navigate = useNavigate();
  return (
    <>
      <PageHead title="Calibrate" sub="One paper per screen, about twenty in a session: half from papers graded relevant or above and half from below, for each Focus topic. Grades, rarity and topic marks are hidden, so the answers are the owner's own." />
      <button className="btn pri" disabled={draw.isPending} onClick={() => draw.mutate(undefined, { onSuccess: (r) => void navigate({ to: "/settings/calibrate/$id", params: { id: String(r.id) } }) })}>
        {draw.isPending ? "Drawing…" : "Start a session"}
      </button>
      <div className="cap">Past sessions</div>
      <Load error={sessions.isError} retry={() => void sessions.refetch()} loading={sessions.isPending} />
      {sessions.data && (
        <div className="list">
          {sessions.data.map((s) => (
            <PastSessionRow key={s.id} id={s.id} drawn={s.drawn_at} answered={s.answered} papers={s.papers} seed={s.seed} />
          ))}
          {!sessions.data.length && <div className="empty">No session yet.</div>}
        </div>
      )}
    </>
  );
}

function PastSessionRow({ id, drawn, answered, papers, seed }: { id: number; drawn: string; answered: number; papers: number; seed: number }) {
  const navigate = useNavigate();
  return (
    <button className="srow" data-sub onClick={() => void navigate({ to: "/settings/calibrate/$id", params: { id: String(id) } })}>
      <span className="t">{rowDate(drawn)}</span>
      <span className="fa faint">
        {answered} of {papers} answered · seed {seed}
      </span>
      <span className="fv faint">{answered < papers ? "unfinished" : ""}</span>
      <span className="fv faint">›</span>
    </button>
  );
}

// -- a session ------------------------------------------------------------------------------------------------------

type Draft = { fit: Record<string, LabelValue>; value: Value | null; note: string };

function draftOf(item: SessionItem): Draft {
  return { fit: { ...item.fit }, value: item.appraisal?.value ?? null, note: item.appraisal?.note ?? "" };
}

/** The paper, blind: title, authors, id, date, Gist, abstract and classes; no grade, rarity or topic marks. */
function BlindPaper({ item }: { item: SessionItem }) {
  const paper = useQuery(paperQuery(item.paper_id));
  const au = authorsShort(item.authors);
  const classes = paper.data?.classification?.classes?.filter((c) => c.member).map((c) => c.name) ?? [];
  return (
    <div className="calp">
      <div className="h1">{item.title}</div>
      <div className="muted calmeta">
        {au.shown.join(", ")}
        {au.more ? ` +${au.more}` : ""} · <span className="mono">{item.paper_id}</span>
        {item.published ? ` · ${rowDate(item.published)}` : ""}
      </div>
      {paper.data?.gist && (
        <div className="rgist up" style={{ "--rc": "var(--fg3)" } as React.CSSProperties}>
          <div className="rl">Gist</div>
          <p>{paper.data.gist.text}</p>
        </div>
      )}
      <div className="rgist up" style={{ "--rc": "var(--fg3)" } as React.CSSProperties}>
        <div className="rl">Abstract</div>
        <p>{item.abstract}</p>
      </div>
      {classes.length > 0 && <div className="faint calcls">Classes: {classes.join(" · ")}</div>}
    </div>
  );
}

function Strip({ items, at, answered, go }: { items: SessionItem[]; at: number; answered: Set<number>; go: (position: number) => void }) {
  return (
    <div className="calprog" role="tablist" aria-label="Papers in the session">
      {items.map((it) => (
        <button key={it.position} className={it.position === at ? "cur" : answered.has(it.position) ? "done" : ""} onClick={() => go(it.position)} title={`Paper ${it.position}`} role="tab" aria-selected={it.position === at}>
          <i />
        </button>
      ))}
    </div>
  );
}

export function SessionPage() {
  const { id } = useParams({ from: "/settings/calibrate/$id" });
  const search = useSearch({ from: "/settings/calibrate/$id" });
  const sessionId = Number(id);
  const session = useQuery(sessionQuery(sessionId));
  if (session.isPending || session.isError) return <Load error={session.isError} retry={() => void session.refetch()} loading />;
  const s = session.data;
  const unanswered = s.items.find((it) => !it.answered_at);
  if (search.at == null) {
    if (!unanswered || search.review) return <Review s={s} />;
    return <Navigate to="/settings/calibrate/$id" params={{ id }} search={{ at: unanswered.position }} replace />;
  }
  const item = s.items.find((it) => it.position === search.at);
  if (!item) return <p className="empty">There is no such paper in the session.</p>;
  return <Screen s={s} item={item} />;
}

function Screen({ s, item }: { s: Session; item: SessionItem }) {
  const navigate = useNavigate();
  const desktop = useDesktop();
  const tree = useQuery(taxonomyQuery);
  const answer = useAnswer(s.id);
  const [draft, setDraft] = useState<Draft>(() => draftOf(item));
  const [pick, setPick] = useState(false);
  useEffect(() => setDraft(draftOf(item)), [item]);
  const focus = (tree.data?.topics ?? []).filter((t) => t.depth === "focus" && t.status === "active");
  const name = (slug: string) => tree.data?.classes.find((c) => c.slug === slug)?.name ?? slug;
  const answered = new Set(s.items.filter((it) => it.answered_at).map((it) => it.position));
  const n = s.items.length;
  const i = s.items.findIndex((it) => it.position === item.position);
  const go = (position: number) => void navigate({ to: "/settings/calibrate/$id", params: { id: String(s.id) }, search: { at: position } });
  const review = () => void navigate({ to: "/settings/calibrate/$id", params: { id: String(s.id) }, search: { review: true } });
  const canRecord = !!draft.fit[item.topic];
  const changed = JSON.stringify(draft) !== JSON.stringify(draftOf(item));
  /** Next records the answer when the drawn topic has its Fit, then moves on; a paper left unanswered stays grey. */
  const next = () => {
    const after = i + 1 < n ? () => go(s.items[i + 1]!.position) : review;
    if (canRecord && (changed || !item.answered_at)) answer.mutate({ position: item.position, answer: { fit: draft.fit, value: draft.value, note: draft.note || null } }, { onSuccess: after });
    else after();
  };
  const back = () => i > 0 && go(s.items[i - 1]!.position);
  const title = `Calibrate · ${item.position} of ${n}`;
  const strip = <Strip items={s.items} at={item.position} answered={answered} go={go} />;
  const picker = (
    <div className="cpk">
      {s.items.map((it) => (
        <button key={it.position} className={it.position === item.position ? "cur" : ""} onClick={() => (setPick(false), go(it.position))}>
          <span className="n">{it.position}</span>
          <span className="t">{it.title}</span>
          <span className="ck">{answered.has(it.position) ? "✓" : ""}</span>
        </button>
      ))}
    </div>
  );
  return (
    <div className="setpg">
      <Fill name="title">
        <span className="t">{title}</span>
      </Fill>
      {strip}
      <BlindPaper item={item} />
      <div className="calqs">
        <div className="calq">
          <div className="cql">Fit, for each Focus topic</div>
          <div>
            {(focus.length ? focus.map((t) => t.class) : [item.topic]).map((slug) => (
              <div className="calfit" key={slug}>
                {focus.find((t) => t.class === slug) ? <Badge topic={{ name: name(slug), ...focus.find((t) => t.class === slug)! }} /> : <span />}
                <span className="nm">
                  {name(slug)}
                  {slug === item.topic && <span className="faint"> · drawn for</span>}
                </span>
                <FitSeg value={draft.fit[slug] ?? null} label={`Fit for ${name(slug)}`} onPick={(f) => setDraft((d) => ({ ...d, fit: { ...d.fit, [slug]: f } }))} />
              </div>
            ))}
          </div>
        </div>
        <div className="calq">
          <div className="cql">Value, whatever the topic</div>
          <div className="fitseg" role="group" aria-label="Value">
            {VALUES.map(({ v, name: vn }) => (
              <button key={v} className={`seg${draft.value === v ? " on" : ""}`} aria-pressed={draft.value === v} onClick={() => setDraft((d) => ({ ...d, value: d.value === v ? null : v }))}>
                {vn}
              </button>
            ))}
          </div>
        </div>
        <div className="calq">
          <div className="cql">Note</div>
          <textarea className="tin" rows={2} value={draft.note} placeholder="Optional" aria-label="Note" onChange={(e) => setDraft((d) => ({ ...d, note: e.target.value }))} />
        </div>
        <Later>"Classes right?" and "Gist and Brief" come with their fields on the answer.</Later>
      </div>
      {desktop ? (
        <div className="calnav">
          <button className="btn" onClick={back} style={{ visibility: i ? "visible" : "hidden" }}>
            ‹ Back
          </button>
          <span className="mono faint">
            {item.position} of {n}
          </span>
          <button className="btn pri" disabled={answer.isPending} onClick={next}>
            {i + 1 === n ? "Done" : "Next ›"}
          </button>
        </div>
      ) : (
        <Fill name="foot">
          <div className="pfoot pgf calf">
            <button className={i ? "" : "dim"} disabled={!i} onClick={back}>
              <small>‹ Back</small>
            </button>
            <button className="lbl" onClick={() => setPick(true)} aria-label="Go to a paper">
              {item.position}/{n} ▴
            </button>
            <button className="on" disabled={answer.isPending} onClick={next}>
              <small>{i + 1 === n ? "Done" : "Next ›"}</small>
            </button>
          </div>
          <Sheet open={pick} onClose={() => setPick(false)} label="Go to a paper">
            <div className="cap">Go to a paper</div>
            {picker}
            <button className="done" onClick={() => setPick(false)}>
              Close
            </button>
          </Sheet>
        </Fill>
      )}
    </div>
  );
}

/** A past or finished session: the owner's Fit against the pipeline's draw per paper, amber where they differ. */
function Review({ s }: { s: Session }) {
  const topics = useQuery(topicsQuery);
  const more = useMore(s.id);
  const navigate = useNavigate();
  const cards = Object.fromEntries((topics.data ?? []).flatMap((g) => g.topics).map((t) => [t.slug, t]));
  const answered = s.items.filter((it) => it.answered_at);
  const off = s.items.filter(differs);
  const left = s.items.length - answered.length;
  const date = rowDate(s.drawn_at);
  return (
    <div className="setpg">
      <PageHead title={`Calibrate · ${date}`} deeper sub={`${answered.length} of ${s.items.length} answered · seed ${s.seed}. The pipeline's side shows here: ${off.length} ${off.length === 1 ? "answer differs" : "answers differ"} from it; they count against precision on Overview.`}>
        {left > 0 && (
          <button className="btn pri" onClick={() => void navigate({ to: "/settings/calibrate/$id", params: { id: String(s.id) }, search: { at: s.items.find((it) => !it.answered_at)!.position } })}>
            Continue · {left} left
          </button>
        )}
        <button className="btn" disabled={more.isPending} onClick={() => more.mutate()}>
          Add the papers waiting for a word
        </button>
      </PageHead>
      <div className="list">
        {s.items.map((it) => {
          const t = cards[it.topic];
          const fit = it.fit[it.topic];
          const side = pipelineSide(it);
          return (
            <PaperLink key={it.position} id={it.paper_id} topic={it.topic} className={`srow${differs(it) ? " off" : ""}${it.answered_at ? "" : " blank"}`}>
              <span className="t">{it.title}</span>
              {t ? <Badge topic={t} size="sm" /> : <span />}
              <span className="fa">
                <span className="faint">you</span> <b>{fit ?? "–"}</b>
              </span>
              <span className="fa">
                <span className="faint">pipeline</span> {side ?? it.why}
              </span>
              <span className="fv faint">{it.appraisal?.value?.replace("_", " ") ?? ""}</span>
            </PaperLink>
          );
        })}
      </div>
    </div>
  );
}
