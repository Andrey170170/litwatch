/** A paper's page (DESIGN.md, "Paper"): the header, its open states, the ladder, and the rail or the phone's bars. */
import { useQuery } from "@tanstack/react-query";
import { useParams, useSearch } from "@tanstack/react-router";
import { useCallback, useEffect, useState } from "react";
import { ApiError } from "@/api/client";
import { useDeepRead, useProcess } from "@/api/mutations";
import { paperQuery } from "@/api/queries";
import { forgetCard, useCardAsked } from "@/lib/remember";
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

function Rail({ paper, gistAway, asked }: { paper: Paper; gistAway: boolean; asked: boolean }) {
  const [miss, setMiss] = useState(false);
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
          {paper.topics.map((t) => (
            <TopicRow key={t.topic.slug} paperId={paper.id} t={t} weight={paper.weight?.weight} revision={paper.revision} />
          ))}
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

export function PaperPage() {
  const { _splat: id = "" } = useParams({ from: "/paper/$" });
  const { topic: under } = useSearch({ from: "/paper/$" });
  // Keyed by the paper: the route is reused from one paper to the next, and no open form, sheet or half-made
  // pick may carry over (a DOI typed on one paper must not be set on another).
  return <PaperView key={id} id={id} under={under} />;
}

function PaperView({ id, under }: { id: string; under: string | undefined }) {
  const desktop = useDesktop();
  const process = useProcess();
  // While a card is asked for, the page polls until an integration has written it (the API has no queued state);
  // likewise while a full-text pass is queued or running, and more slowly while it awaits its text, so the Brief
  // appears once written. A pass waiting on the owner (its note above) is not polled for.
  const asked = useCardAsked(id);
  const read = useQuery({
    ...paperQuery(id),
    refetchInterval: (q) => {
      const p = q.state.data;
      const stuck = (p?.states ?? []).some((s) => s.kind === "fulltext_failed" || s.kind === "awaiting_text");
      if (asked || p?.text_pass?.state === "ready" || p?.text_pass?.state === "running") return 15_000;
      return p?.text_pass?.state === "awaiting_text" && !stuck ? 60_000 : false;
    },
  });
  const { data: paper, error, isPending } = read;
  const [gistAway, setGistAway] = useState(false);
  const [sheet, setSheet] = useState<"more" | "up" | "down" | null>(null);
  useEffect(() => {
    if (asked && paper?.card) forgetCard(id);
  }, [asked, paper?.card, id]);
  const onGistAway = useCallback((away: boolean) => setGistAway(away), []);
  const onLabel = (topic: string) => {
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
        </div>
      </>
    );
  }
  const deepRead = paper.card_requestable && !asked ? <DeepReadInline paper={paper} /> : null;
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
        {desktop && <Rail paper={paper} gistAway={gistAway} asked={asked} />}
      </div>
      {!desktop && <PhoneBar paper={paper} under={under} asked={asked} sheet={sheet} setSheet={setSheet} />}
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
