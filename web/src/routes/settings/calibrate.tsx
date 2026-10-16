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
import { answerOf, differs, type Edits, type LabelValue, pipelineSide, type Session, type SessionItem, type SessionRow, type Value } from "@/lib/settings";
import { BriefBody } from "@/components/paper/Ladder";
import { Failed, FitSeg, Later, Load, PageHead, PaperLink, useTopicCards } from "./parts";

const VALUES: Value[] = ["matters", "fine", "not_for_me"];

/** The way to a session: its first unanswered paper, a paper by position, or its review. Moves within a session
 * pass `replace`, so Back leaves the session rather than walking through its papers. */
function useOpenSession() {
  const navigate = useNavigate();
  return (id: number, search: { at?: number; review?: boolean } = {}, replace = false) => void navigate({ to: "/settings/calibrate/$id", params: { id: String(id) }, search, replace });
}

export function CalibratePage() {
  const sessions = useQuery(sessionsQuery("calibrate"));
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
    <button className="sess" data-sub onClick={() => open(s.id, { review: true })}>
      <span className="t">{rowDate(s.drawn_at)}</span>
      <span className="fa faint">
        {s.answered} of {s.papers} answered · {s.differ} differ · seed {s.seed}
      </span>
      <span className="fv faint">{s.answered < s.papers ? "unfinished" : ""}</span>
      <span className="fv faint">›</span>
    </button>
  );
}

// -- a session ------------------------------------------------------------------------------------------------------

/** The paper, blind: title, authors, id, date, Gist, Brief or abstract, and classes; no grade, rarity or topic marks. */
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
        {paper.data?.brief ? (
          <>
            <div className="rl">Brief</div>
            <BriefBody paper={paper.data} brief={paper.data.brief} />
          </>
        ) : (
          <>
            <div className="rl">Abstract</div>
            <p>{item.abstract}</p>
          </>
        )}
      </div>
      {classes.length > 0 && <div className="faint calcls">Classes: {classes.join(" · ")}</div>}
      <Failed q={paper} what="the paper's Gist, Brief and classes" />
    </div>
  );
}

export function SessionPage() {
  const { id } = useParams({ from: "/settings/calibrate/$id" });
  const search = useSearch({ from: "/settings/calibrate/$id" });
  const session = useQuery(sessionQuery(Number(id)));
  // The session shows on the cached read while a fresh visit refetches it ("Updating…" above).
  return (
    <>
      <Load q={session} />
      {session.data && <SessionBody s={session.data} id={id} at={search.at} review={search.review} />}
    </>
  );
}

function SessionBody({ s, id, at, review }: { s: Session; id: string; at: number | undefined; review: boolean | undefined }) {
  const unanswered = s.items.find((it) => !it.answered_at);
  if (at == null) {
    if (!unanswered || review) return <Review s={s} />;
    return <Navigate to="/settings/calibrate/$id" params={{ id }} search={{ at: unanswered.position }} replace />;
  }
  const item = s.items.find((it) => it.position === at);
  if (!item) return <p className="empty">There is no such paper in the session.</p>;
  // Keyed by the session and the position: each paper starts from its own recorded answer, whatever screen showed
  // before.
  return <Screen key={`${s.id}:${item.position}`} s={s} item={item} />;
}

/** One paper's screen. The draft is the item as last read (the baseline, refreshed under the screen by the
 * session's reads) plus what the owner changed on it (`Edits`): a changed field stands over a refresh, an untouched
 * one follows it, and `answerOf` sends only what was changed. The drawn topic's recorded answer shows faint where
 * no label stands now: history, not a label to write again. The whole editor and its navigation wait while an
 * answer is saved, so nothing typed meanwhile is lost and no two saves overlap. */
function Screen({ s, item }: { s: Session; item: SessionItem }) {
  const open = useOpenSession();
  const desktop = useDesktop();
  const tree = useQuery(taxonomyQuery);
  const answer = useAnswer(item.paper_id);
  const [edits, setEdits] = useState<Edits>({ fit: {} });
  // Next or a move found edits but no Fit for the drawn topic, which a first answer needs: the screen asks for it.
  const [missing, setMissing] = useState(false);
  const [pick, setPick] = useState(false);
  const name = (slug: string) => tree.data?.classes.find((c) => c.slug === slug)?.name ?? slug;
  // The drawn topic first, paused or not, then every other active Focus topic; the answer takes Focus topics only.
  const topics = tree.data?.topics ?? [];
  const drawn = topics.find((t) => t.class === item.topic);
  const asked: (Partial<Schemas["Topic"]> & { class: string })[] = [drawn ?? { class: item.topic }, ...topics.filter((t) => t.class !== item.topic && t.depth === "focus" && t.status === "active")];
  const focus = new Set(topics.filter((t) => t.depth === "focus").map((t) => t.class));
  const answered = new Set(s.items.filter((it) => it.answered_at).map((it) => it.position));
  const n = s.items.length;
  const i = s.items.findIndex((it) => it.position === item.position);
  const saving = answer.isPending;
  const go = (position: number) => open(s.id, { at: position }, true);
  const shown = { fit: (slug: string) => edits.fit[slug] ?? item.fit[slug] ?? null, value: edits.value === undefined ? (item.appraisal?.value ?? null) : edits.value, note: edits.note ?? item.appraisal?.note ?? "" };
  const { answer: body, edited, complete } = answerOf(item, edits, focus);
  /** A Fit chosen is an edit; the chosen one tapped again takes the edit back. */
  const setFit = (slug: string, f: LabelValue | null) =>
    setEdits((d) => {
      const fit = { ...d.fit };
      if (f) fit[slug] = f;
      else delete fit[slug];
      return { ...d, fit };
    });
  /** Record the answer, then move on: what was edited, and on Next a first answer with the drawn topic's Fit as it
   * stands (`wanted`). Edits without that Fit stay on the screen and ask for it; an untouched paper is left as it
   * was. Nothing moves while a save is in flight. */
  const leave = (wanted: boolean, after: () => void) => {
    if (saving) return;
    if (!complete) return edited ? setMissing(true) : after();
    if (!edited && !wanted) return after();
    answer.mutate({ sessionId: s.id, position: item.position, answer: body }, { onSuccess: after });
  };
  /** Next records a first answer when the drawn topic has its Fit, and any edit; a paper left untouched stays grey. */
  const next = () => leave(!item.answered_at, i + 1 < n ? () => go(s.items[i + 1]!.position) : () => open(s.id, { review: true }, true));
  /** Back, the strip and the picker keep what was changed on this paper before moving; an untouched one stays as it was. */
  const move = (position: number) => leave(false, () => go(position));
  const back = () => i > 0 && move(s.items[i - 1]!.position);
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
          <button key={it.position} className={it.position === item.position ? "cur" : answered.has(it.position) ? "done" : ""} disabled={saving} onClick={() => move(it.position)} title={`Paper ${it.position}`} role="tab" aria-selected={it.position === item.position}>
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
                  {t.class === item.topic && item.answer && !item.fit[item.topic] && <span className="faint"> · answered {item.answer} before, no label stands</span>}
                </span>
                <FitSeg value={shown.fit(t.class)} auto={t.class === item.topic && !item.fit[item.topic] ? item.answer : null} label={`Fit for ${name(t.class)}`} disabled={saving} clear={t.class in edits.fit} onPick={(f) => setFit(t.class, f)} />
              </div>
            ))}
            <Failed q={tree} what="the other Focus topics" />
            {missing && !complete && (
              <div className="nb warnt" role="alert">
                Choose the Fit for {name(item.topic)} to record this paper; it is the one it was drawn for.
              </div>
            )}
          </div>
        </div>
        <div className="calq">
          <div className="cql">Value, whatever the topic</div>
          <Seg values={VALUES} value={shown.value} clear disabled={saving} label="Value" onPick={(value) => setEdits((d) => ({ ...d, value }))} />
        </div>
        <div className="calq">
          <div className="cql">Note</div>
          <textarea className="tin" rows={2} value={shown.note} disabled={saving} placeholder="Optional" aria-label="Note" onChange={(e) => setEdits((d) => ({ ...d, note: e.target.value }))} />
        </div>
        <Later>"Classes right?" and "Gist and Brief" come with their fields on the answer.</Later>
      </div>
      {desktop ? (
        <div className="calnav">
          <button className="btn" disabled={saving} onClick={back} style={{ visibility: i ? "visible" : "hidden" }}>
            ‹ Back
          </button>
          <span className="mono faint">
            {item.position} of {n}
          </span>
          <button className="btn pri" disabled={saving} onClick={next}>
            {saving ? "Saving…" : last ? "Done" : "Next ›"}
          </button>
        </div>
      ) : (
        <Fill name="foot">
          <div className="pfoot pgf calf">
            <button className={i ? "" : "dim"} disabled={!i || saving} onClick={back}>
              <small>‹ Back</small>
            </button>
            <button className="lbl" disabled={saving} onClick={() => setPick(true)} aria-label="Go to a paper">
              {item.position}/{n} ▴
            </button>
            <button className="on" disabled={saving} onClick={next}>
              <small>{saving ? "Saving…" : last ? "Done" : "Next ›"}</small>
            </button>
          </div>
          <Sheet open={pick} onClose={() => setPick(false)} label="Go to a paper">
            <div className="cap">Go to a paper</div>
            <div className="cpk">
              {s.items.map((it) => (
                <button key={it.position} className={it.position === item.position ? "cur" : ""} disabled={saving} onClick={() => (setPick(false), move(it.position))}>
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
      <PageHead title={`Calibrate · ${rowDate(s.drawn_at)}`} deeper sub={`${s.items.length - unanswered.length} of ${s.items.length} answered · seed ${s.seed}. The pipeline's Fit shows here: ${off.length} ${off.length === 1 ? "answer differs" : "answers differ"} from it; an answer on its other side counts against precision on Overview.`}>
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
                <span className="faint">you</span> <b>{it.answer ?? "–"}</b>
              </span>
              <span className="fa" title={it.verdict ?? pipelineSide(it) ?? it.why ?? undefined}>
                <span className="faint">pipeline</span> {it.verdict ?? pipelineSide(it) ?? it.why}
              </span>
              <span className="fv faint">{it.appraisal?.value?.replace("_", " ") ?? ""}</span>
            </PaperLink>
          );
        })}
      </div>
    </div>
  );
}
