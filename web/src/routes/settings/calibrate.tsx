/**
 * Calibrate (DESIGN.md, "Calibrate"): a first page with "Start a session" and the past sessions; a session's screen,
 * one paper at a time, blind, with the strip and the questions; a session done or opened later shows its review,
 * the owner's Fit against the pipeline's draw, amber where they differ.
 */
import { useQuery } from "@tanstack/react-query";
import { Navigate, useNavigate, useParams, useSearch } from "@tanstack/react-router";
import { useState } from "react";
import type { Schemas } from "@/api/client";
import { useAnswer, useDrawSession, useMore } from "@/api/mutations";
import { paperQuery, sessionQuery, sessionsQuery, taxonomyQuery } from "@/api/queries";
import { Badge } from "@/components/Badge";
import { Seg } from "@/components/Seg";
import { Sheet } from "@/components/Sheet";
import { Fill } from "@/components/slots";
import { useDesktop } from "@/hooks/useDesktop";
import { authorsShort, rowDate } from "@/lib/format";
import { differs, type LabelValue, pipelineSide, type Session, type SessionItem, type SessionRow } from "@/lib/settings";
import { FitSeg, Later, Load, PageHead, PaperLink, useTopicCards } from "./parts";

type Value = NonNullable<Schemas["Answer"]["value"]>;
const VALUES: Value[] = ["matters", "fine", "not_for_me"];

/** The way to a session: its first unanswered paper, a paper by position, or its review. */
function useOpenSession() {
  const navigate = useNavigate();
  return (id: number, search: { at?: number; review?: boolean } = {}) => void navigate({ to: "/settings/calibrate/$id", params: { id: String(id) }, search });
}

export function CalibratePage() {
  const sessions = useQuery(sessionsQuery);
  const draw = useDrawSession();
  const open = useOpenSession();
  return (
    <>
      <PageHead title="Calibrate" sub="One paper per screen, about twenty in a session: half from papers graded relevant or above and half from below, for each Focus topic. Grades, rarity and topic marks are hidden, so the answers are the owner's own." />
      <button className="btn pri" disabled={draw.isPending} onClick={() => draw.mutate(undefined, { onSuccess: (r) => r.id != null && open(r.id) })}>
        {draw.isPending ? "Drawing…" : "Start a session"}
      </button>
      <div className="cap">Past sessions</div>
      <Load q={sessions} />
      {sessions.data && (
        <div className="list">
          {sessions.data.map((s) => (
            <PastSessionRow key={s.id} s={s} />
          ))}
          {!sessions.data.length && <div className="empty">No session yet.</div>}
        </div>
      )}
    </>
  );
}

function PastSessionRow({ s }: { s: SessionRow }) {
  const open = useOpenSession();
  return (
    <button className="sess" data-sub onClick={() => open(s.id)}>
      <span className="t">{rowDate(s.drawn_at)}</span>
      <span className="fa faint">
        {s.answered} of {s.papers} answered · seed {s.seed}
      </span>
      <span className="fv faint">{s.answered < s.papers ? "unfinished" : ""}</span>
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
        <div className="rgist up">
          <div className="rl">Gist</div>
          <p>{paper.data.gist.text}</p>
        </div>
      )}
      <div className="rgist up">
        <div className="rl">Abstract</div>
        <p>{item.abstract}</p>
      </div>
      {classes.length > 0 && <div className="faint calcls">Classes: {classes.join(" · ")}</div>}
    </div>
  );
}

export function SessionPage() {
  const { id } = useParams({ from: "/settings/calibrate/$id" });
  const search = useSearch({ from: "/settings/calibrate/$id" });
  const session = useQuery(sessionQuery(Number(id)));
  if (!session.data) return <Load q={session} />;
  const s = session.data;
  const unanswered = s.items.find((it) => !it.answered_at);
  if (search.at == null) {
    if (!unanswered || search.review) return <Review s={s} />;
    return <Navigate to="/settings/calibrate/$id" params={{ id }} search={{ at: unanswered.position }} replace />;
  }
  const item = s.items.find((it) => it.position === search.at);
  if (!item) return <p className="empty">There is no such paper in the session.</p>;
  // Keyed by position: each paper starts from its own recorded answer.
  return <Screen key={item.position} s={s} item={item} />;
}

function Screen({ s, item }: { s: Session; item: SessionItem }) {
  const open = useOpenSession();
  const desktop = useDesktop();
  const tree = useQuery(taxonomyQuery);
  const answer = useAnswer(s.id);
  const [draft, setDraft] = useState<Draft>(() => draftOf(item));
  const [pick, setPick] = useState(false);
  const name = (slug: string) => tree.data?.classes.find((c) => c.slug === slug)?.name ?? slug;
  // Each active Focus topic gets its Fit; the drawn topic at least, while the tree loads.
  const focus = (tree.data?.topics ?? []).filter((t) => t.depth === "focus" && t.status === "active");
  const asked = focus.length ? focus : [{ class: item.topic } as Partial<Schemas["Topic"]> & { class: string }];
  const answered = new Set(s.items.filter((it) => it.answered_at).map((it) => it.position));
  const n = s.items.length;
  const i = s.items.findIndex((it) => it.position === item.position);
  const go = (position: number) => open(s.id, { at: position });
  const canRecord = !!draft.fit[item.topic];
  const changed = JSON.stringify(draft) !== JSON.stringify(draftOf(item));
  /** Next records the answer when the drawn topic has its Fit, then moves on; a paper left unanswered stays grey. */
  const next = () => {
    const after = i + 1 < n ? () => go(s.items[i + 1]!.position) : () => open(s.id, { review: true });
    if (canRecord && (changed || !item.answered_at)) answer.mutate({ position: item.position, answer: { fit: draft.fit, value: draft.value, note: draft.note || null } }, { onSuccess: after });
    else after();
  };
  const back = () => i > 0 && go(s.items[i - 1]!.position);
  const last = i + 1 === n;
  return (
    <div className="setpg">
      <Fill name="title">
        <span className="t">
          Calibrate · {item.position} of {n}
        </span>
      </Fill>
      <div className="calprog" role="tablist" aria-label="Papers in the session">
        {s.items.map((it) => (
          <button key={it.position} className={it.position === item.position ? "cur" : answered.has(it.position) ? "done" : ""} onClick={() => go(it.position)} title={`Paper ${it.position}`} role="tab" aria-selected={it.position === item.position}>
            <i />
          </button>
        ))}
      </div>
      <BlindPaper item={item} />
      <div className="calqs">
        <div className="calq">
          <div className="cql">Fit, for each Focus topic</div>
          <div>
            {asked.map((t) => (
              <div className="calfit" key={t.class}>
                {t.depth ? <Badge topic={{ name: name(t.class), depth: t.depth, monogram: t.monogram!, colour: t.colour! }} /> : <span />}
                <span className="nm">
                  {name(t.class)}
                  {t.class === item.topic && <span className="faint"> · drawn for</span>}
                </span>
                <FitSeg value={draft.fit[t.class] ?? null} label={`Fit for ${name(t.class)}`} onPick={(f) => setDraft((d) => ({ ...d, fit: { ...d.fit, [t.class]: f } }))} />
              </div>
            ))}
          </div>
        </div>
        <div className="calq">
          <div className="cql">Value, whatever the topic</div>
          <Seg values={VALUES} value={draft.value} clear label="Value" onPick={(value) => setDraft((d) => ({ ...d, value }))} />
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
            {last ? "Done" : "Next ›"}
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
              <small>{last ? "Done" : "Next ›"}</small>
            </button>
          </div>
          <Sheet open={pick} onClose={() => setPick(false)} label="Go to a paper">
            <div className="cap">Go to a paper</div>
            <div className="cpk">
              {s.items.map((it) => (
                <button key={it.position} className={it.position === item.position ? "cur" : ""} onClick={() => (setPick(false), go(it.position))}>
                  <span className="n">{it.position}</span>
                  <span className="t">{it.title}</span>
                  <span className="ck">{answered.has(it.position) ? "✓" : ""}</span>
                </button>
              ))}
            </div>
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
  const cards = useTopicCards();
  const more = useMore(s.id);
  const open = useOpenSession();
  const unanswered = s.items.filter((it) => !it.answered_at);
  const off = s.items.filter(differs);
  return (
    <div className="setpg">
      <PageHead title={`Calibrate · ${rowDate(s.drawn_at)}`} deeper sub={`${s.items.length - unanswered.length} of ${s.items.length} answered · seed ${s.seed}. The pipeline's side shows here: ${off.length} ${off.length === 1 ? "answer differs" : "answers differ"} from it; they count against precision on Overview.`}>
        {unanswered.length > 0 && (
          <button className="btn pri" onClick={() => open(s.id, { at: unanswered[0]!.position })}>
            Continue · {unanswered.length} left
          </button>
        )}
        <button className="btn" disabled={more.isPending} onClick={() => more.mutate()}>
          Add the papers waiting for a word
        </button>
      </PageHead>
      <div className="list">
        {s.items.map((it) => {
          const t = cards[it.topic];
          return (
            <PaperLink key={it.position} id={it.paper_id} topic={it.topic} className={`sess${differs(it) ? " off" : ""}${it.answered_at ? "" : " blank"}`}>
              <span className="t">{it.title}</span>
              {t ? <Badge topic={t} size="sm" /> : <span />}
              <span className="fa">
                <span className="faint">you</span> <b>{it.fit[it.topic] ?? "–"}</b>
              </span>
              <span className="fa">
                <span className="faint">pipeline</span> {pipelineSide(it) ?? it.why}
              </span>
              <span className="fv faint">{it.appraisal?.value?.replace("_", " ") ?? ""}</span>
            </PaperLink>
          );
        })}
      </div>
    </div>
  );
}
