/** A paper's page (DESIGN.md, "Paper"): the header, its open states, the ladder, and the rail or the phone's bars. */
import { useQuery } from "@tanstack/react-query";
import { useLocation, useNavigate, useParams, useSearch } from "@tanstack/react-router";
import { useCallback, useEffect, useState } from "react";
import { flushSync } from "react-dom";
import { ApiError } from "@/api/client";
import { useDeepRead, useProcess } from "@/api/mutations";
import { processing } from "@/lib/jobs";
import { paperQuery } from "@/api/queries";
import { placeOf } from "@/lib/listContext";
import { cardAnswered, forgetCard, useCardAsked } from "@/lib/remember";
import { useSteps } from "@/lib/steps";
import { Back } from "@/components/Shell";
import { Fill } from "@/components/slots";
import { Failed } from "@/routes/settings/parts";
import { useDesktop } from "@/hooks/useDesktop";
import { rc } from "@/lib/rarity";
import { DeepReadCell, linkCells, MissPicker, ProcessCell, SaveCell, type Paper } from "@/components/paper/Actions";
import { Header } from "@/components/paper/Header";
import { Ladder } from "@/components/paper/Ladder";
import { Notes } from "@/components/paper/Notes";
import { PhoneBar } from "@/components/paper/Phone";
import { PaperRow, TopicRow } from "@/components/paper/TopicRow";
import { folded, FoldedTopics, type Topics, useSettledTopics } from "@/components/paper/Fold";

function Rail({ paper, topics, gistAway, asked, missOpen }: { paper: Paper; topics: Topics; gistAway: boolean; asked: boolean; missOpen: boolean }) {
  const [miss, setMiss] = useState(missOpen);
  const analysisFailed = (paper.states ?? []).some((s) => s.kind === "analysis_stopped");
  return (
    <aside className="pgrail">
      {paper.gist && !analysisFailed && (
        <div className={`rgist${gistAway ? " up" : ""}`} style={{ "--rc": rc(paper.rarity) } as React.CSSProperties} aria-hidden={!gistAway}>
          <div className="rl">
            Gist
          </div>
          <p>{paper.gist.text}</p>
        </div>
      )}
      {paper.in_collection && (
        <div className="tplist" id="rail-topics">
          <FoldedTopics {...topics} row={(t) => <TopicRow key={t.topic.slug} paperId={paper.id} t={t} weight={paper.weight?.weight} revision={paper.revision} />} />
          {!paper.topics.length && <PaperRow paperId={paper.id} reaction={paper.reaction} />}
        </div>
      )}
      {paper.in_collection ? (
        <div className="agrid">
          <SaveCell paper={paper} />
          {linkCells(paper, ["bib", "arxiv", "pdf"])}
          <DeepReadCell paper={paper} asked={asked} wide />
        </div>
      ) : (
        <>
          <div className="agrid">
            <ProcessCell paper={paper} />
            {linkCells(paper, ["bib"])}
            <DeepReadCell paper={paper} asked={asked} wide />
            <button className="wide" onClick={() => setMiss((m) => !m)}>
              Report as a miss
            </button>
          </div>
          {miss && <MissPicker paper={paper} onDone={() => setMiss(false)} />}
        </>
      )}
    </aside>
  );
}

/** How often the page polls: while a card is asked for, the paper's Process job is under way, or the Brief's or the
 * Deep card's pass is queued or running, and more slowly while one awaits its text. A pass waiting on the owner (an open state names it) is not polled for. */
export function pollEvery(p: Paper | undefined, asked: boolean): number | false {
  const owner = new Set((p?.states ?? []).map((s) => s.pass_id));
  const passes = [p?.text_pass, p?.card_pass].filter((x): x is NonNullable<typeof x> => x != null && !owner.has(x.id));
  if (asked || processing(p?.process) || passes.some((x) => x.state === "ready" || x.state === "running")) return 15_000;
  return passes.some((x) => x.state === "awaiting_text") ? 60_000 : false;
}

export function PaperPage() {
  const { _splat: id = "" } = useParams({ from: "/paper/$" });
  const { topic: under, miss } = useSearch({ from: "/paper/$" });
  // Keyed by the paper: the route is reused from one paper to the next, and no open form, sheet or half-made
  // pick may carry over (a DOI typed on one paper must not be set on another).
  return <PaperView key={id} id={id} under={under} miss={!!miss} />;
}

/** The phone's swipe steps to the previous or next paper of the list this one was opened from (`listContext`),
 * under the same topic and list, as a history entry. */
function usePaperSteps(id: string, under: string | undefined) {
  const navigate = useNavigate();
  const listKey = useLocation({ select: (l) => l.state.list });
  const place = placeOf(id, listKey);
  const go = (to: string | null | undefined) => (to ? () => void navigate({ to: "/paper/$", params: { _splat: to }, search: under ? { topic: under } : {}, state: { list: listKey } }) : null);
  useSteps(place ? { prev: go(place.prev), next: go(place.next) } : null);
}

/** `miss`: the page was reached by the share target (`routes/share.tsx`), so "Report as a miss" opens at once. */
function PaperView({ id, under, miss }: { id: string; under: string | undefined; miss: boolean }) {
  const desktop = useDesktop();
  const process = useProcess();
  usePaperSteps(id, under);
  // While a card is asked for, the page polls until an integration has written it (the API has no queued state);
  // likewise while a pass is queued or running, so its Brief or card appears once written (`pollEvery`).
  const asked = useCardAsked(id);
  const read = useQuery({
    ...paperQuery(id),
    refetchInterval: (q) => pollEvery(q.state.data, asked),
  });
  const { data: paper, error, isPending } = read;
  const [gistAway, setGistAway] = useState(false);
  const settled = useSettledTopics(paper?.topics);
  const [open, setOpen] = useState(false);
  const topics: Topics = { topics: settled, open, setOpen };
  const [sheet, setSheet] = useState<"more" | "up" | "down" | null>(miss ? "more" : null);
  // Report as a miss for a paper neither litwatch nor the archive has (the report fetches it), open at once
  // from the share target
  const [missPick, setMissPick] = useState(miss);
  // The request is remembered until the page reads its pass back, or a card newer than the one asked over.
  useEffect(() => {
    if (asked && paper && cardAnswered(id, paper.card, paper.card_pass)) forgetCard(id);
  }, [asked, paper, id]);
  const onGistAway = useCallback((away: boolean) => setGistAway(away), []);
  const onLabel = (topic: string) => {
    // a folded topic's row is rendered before it is scrolled to
    if (folded(settled).some((t) => t.topic.slug === topic)) flushSync(() => setOpen(true));
    if (!desktop) setSheet("more");
    else document.getElementById("rail-topics")?.querySelector<HTMLElement>(`[data-topic="${topic}"]`)?.scrollIntoView({ block: "nearest" });
  };
  if (isPending) return <div className="loadmore">Loading…</div>;
  // A failed poll while a card is awaited keeps the page: only a paper that never loaded shows the error.
  // A paper neither litwatch nor the archive has (or no archive is configured): Process asks arXiv for it.
  // Anything else (the archive did not answer, a 503) says nothing of the paper: try again.
  if (!paper) {
    const unheld = error instanceof ApiError && error.status === 404;
    if (!unheld)
      return (
        <>
          <Back />
          <Failed q={read} what="the paper" says={error instanceof ApiError && error.status === 503 ? error.message : undefined} />
        </>
      );
    return (
      <>
        <Back />
        <div className="empty">{`${id} is not in the collection.`}</div>
        <div className="agrid" style={{ maxWidth: 320 }}>
          <button className="pri" onClick={() => process.mutate(id)} disabled={process.isPending}>
            Process
          </button>
          <button onClick={() => setMissPick(!missPick)} aria-pressed={missPick}>
            Report as a miss
          </button>
        </div>
        {missPick && <MissPicker paper={{ id }} onDone={() => setMissPick(false)} />}
      </>
    );
  }
  const deepRead = paper.card_requestable && !asked && !paper.card_pass ? <DeepReadInline paper={paper} /> : null;
  return (
    <div className="pg">
      <Fill name="title">
        <span className="t">{paper.title}</span>
      </Fill>
      <Back />
      <Header paper={paper} />
      <Notes paper={paper} onLabel={onLabel} />
      <div className="pgcols">
        <Ladder paper={paper} asked={asked} deepRead={deepRead} onGistAway={onGistAway} />
        {desktop && <Rail paper={paper} topics={topics} gistAway={gistAway} asked={asked} missOpen={miss && !paper.in_collection} />}
      </div>
      {!desktop && <PhoneBar paper={paper} topics={topics} under={under} asked={asked} sheet={sheet} setSheet={setSheet} missOpen={miss && !paper.in_collection} />}
    </div>
  );
}

/** "Deep read" inline in the Deep card rung's line, while the paper has no card. */
function DeepReadInline({ paper }: { paper: Paper }) {
  const deep = useDeepRead(paper.id);
  return (
    <button className="inl" onClick={() => deep.mutate(undefined)} disabled={deep.isPending}>
      Deep read
    </button>
  );
}
