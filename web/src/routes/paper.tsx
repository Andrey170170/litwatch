/** A paper's page (DESIGN.md, "Paper"): the header, its open states, the ladder, and the rail or the phone's bars. */
import { useQuery } from "@tanstack/react-query";
import { useParams, useSearch } from "@tanstack/react-router";
import { useCallback, useEffect, useState } from "react";
import { ApiError } from "@/api/client";
import { useDeepRead, useProcess } from "@/api/mutations";
import { paperQuery } from "@/api/queries";
import { Back } from "@/components/Shell";
import { Fill } from "@/components/slots";
import { useDesktop } from "@/hooks/useDesktop";
import { rc } from "@/lib/rarity";
import { DeepReadCell, linkCells, MissPicker, SaveCell, type Paper } from "@/components/paper/Actions";
import { Header } from "@/components/paper/Header";
import { Ladder, Rate } from "@/components/paper/Ladder";
import { Notes } from "@/components/paper/Notes";
import { PhoneBar } from "@/components/paper/Phone";
import { TopicRow } from "@/components/paper/TopicRow";

function Rail({ paper, gistAway, asked, onAsked }: { paper: Paper; gistAway: boolean; asked: boolean; onAsked: () => void }) {
  const [gistRate, setGistRate] = useState<"ok" | "off" | null>(null);
  const [miss, setMiss] = useState(false);
  const process = useProcess();
  const analysisFailed = (paper.states ?? []).some((s) => s.kind === "analysis_stopped");
  return (
    <aside className="pgrail">
      {paper.gist && !analysisFailed && (
        <div className={`rgist${gistAway ? " up" : ""}`} style={{ "--rc": rc(paper.rarity) } as React.CSSProperties} aria-hidden={!gistAway}>
          <div className="rl">
            Gist
            <Rate what="Gist" value={gistRate} onChange={setGistRate} />
          </div>
          <p>{paper.gist.text}</p>
        </div>
      )}
      {paper.in_collection && paper.topics.length > 0 && (
        <div className="tplist" id="rail-topics">
          {paper.topics.map((t) => (
            <TopicRow key={t.topic.slug} paperId={paper.id} t={t} weight={paper.weight?.weight} revision={paper.revision} />
          ))}
        </div>
      )}
      {paper.in_collection ? (
        <div className="agrid">
          <SaveCell paper={paper} />
          {linkCells(paper, ["bib", "arxiv", "pdf"])}
          <DeepReadCell paper={paper} asked={asked} onAsked={onAsked} wide />
        </div>
      ) : (
        <>
          <div className="agrid">
            <button className="pri" onClick={() => process.mutate(paper.id)} disabled={process.isPending}>
              Process
            </button>
            <DeepReadCell paper={paper} asked={asked} onAsked={onAsked} />
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
  const desktop = useDesktop();
  const { data: paper, error, isPending } = useQuery(paperQuery(id));
  const [gistAway, setGistAway] = useState(false);
  const [asked, setAsked] = useState(false);
  const [sheet, setSheet] = useState<"more" | "up" | "down" | null>(null);
  useEffect(() => {
    setAsked(false);
    setSheet(null);
    setGistAway(false);
  }, [id]);
  const onGistAway = useCallback((away: boolean) => setGistAway(away), []);
  const onLabel = (topic: string) => {
    if (!desktop) setSheet("more");
    else document.getElementById("rail-topics")?.querySelector<HTMLElement>(`[data-topic="${topic}"]`)?.scrollIntoView({ block: "nearest" });
  };
  if (isPending) return <div className="loadmore">Loading…</div>;
  if (error || !paper)
    return (
      <>
        <Back />
        <div className="empty">{error instanceof ApiError && error.status === 404 ? `${id} is not in the collection.` : "Could not load the paper."}</div>
      </>
    );
  const deepRead = paper.card_requestable && !asked ? <DeepReadInline paper={paper} onAsked={() => setAsked(true)} /> : null;
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
        {desktop && <Rail paper={paper} gistAway={gistAway} asked={asked} onAsked={() => setAsked(true)} />}
      </div>
      {!desktop && <PhoneBar paper={paper} under={under} asked={asked} onAsked={() => setAsked(true)} sheet={sheet} setSheet={setSheet} />}
    </div>
  );
}

/** "Deep read" inline in the Deep card rung's line, while the paper has no card. */
function DeepReadInline({ paper, onAsked }: { paper: Paper; onAsked: () => void }) {
  const deep = useDeepRead(paper.id);
  return (
    <button className="inl" onClick={() => deep.mutate(undefined, { onSuccess: onAsked })} disabled={deep.isPending}>
      Deep read
    </button>
  );
}
