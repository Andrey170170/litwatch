/**
 * The paper on the phone (DESIGN.md, "On the phone"): the bottom bar (Save, the thumbs, its place, ⋯), the
 * reaction sheet with a topic choice first, and the paper sheet with a label per topic, the links and the facts.
 */
import { useState } from "react";
import { useLocation } from "@tanstack/react-router";
import { useDeepRead, useSave } from "@/api/mutations";
import { Badge } from "@/components/Badge";
import { Sheet } from "@/components/Sheet";
import { Fill } from "@/components/slots";
import { placeOf } from "@/lib/listContext";
import { gradeLine, rc } from "@/lib/rarity";
import type { ReactionValue } from "@/lib/reactions";
import { DeepReadCell, linkCells, MissPicker, ProcessCell, type Paper } from "./Actions";
import { Classes, Published } from "./Ladder";
import { FitControl, Reasons, whoseFit, type PaperTopic } from "./TopicRow";

/** The topic row a reaction sheet opens on: the one the paper was opened under, else its only one; none when it
 * must ask. */
const sheetTopic = (paper: Paper, under: string | undefined) => paper.topics.find((t) => t.topic.slug === under) ?? (paper.topics.length === 1 ? paper.topics[0] : undefined);

function ReactSheet({ paper, value, under, onClose }: { paper: Paper; value: ReactionValue; under: string | undefined; onClose: () => void }) {
  const topics = paper.topics;
  const [topic, setTopic] = useState<string | null>(sheetTopic(paper, under)?.topic.slug ?? null);
  const current = topic ? paper.topics.find((t) => t.topic.slug === topic)?.reaction : paper.reaction;
  return (
    <Sheet open onClose={onClose} label={value === "up" ? "Thumbs up" : "Thumbs down"}>
      {topics.length > 1 && (
        <>
          <div className="cap">For which topic</div>
          <div className="srow">
            {topics.map((t) => (
              <button key={t.topic.slug} className={`fchip${topic === t.topic.slug ? " on" : ""}`} onClick={() => setTopic(t.topic.slug)} aria-pressed={topic === t.topic.slug}>
                <Badge topic={t.topic} size="sm" /> {t.topic.name}
              </button>
            ))}
          </div>
        </>
      )}
      {(topic || !topics.length) && (
        <>
          <div className="cap">{value === "up" ? "👍 Why" : "👎 What went wrong"}</div>
          <Reasons paperId={paper.id} topic={topic} value={value} current={current} onDone={onClose} big />
        </>
      )}
      <button className="done" onClick={onClose}>
        Done
      </button>
    </Sheet>
  );
}

function Slab({ paper, t }: { paper: Paper; t: PaperTopic }) {
  const asks = t.state === "split" || t.state === "escalated";
  const failed = t.state === "ungraded";
  const said = t.verdict ? Object.entries(t.verdict.opinions ?? {}).filter(([, o]) => o.fit).map(([role, o]) => `${role} ${o.fit}`) : [];
  return (
    <div className={`slab${asks ? " ask" : ""}`} style={{ "--rc": failed ? "var(--fg3)" : rc(t.rarity) } as React.CSSProperties}>
      <div className="sh">
        <Badge topic={t.topic} />
        <span className="nm">{t.topic.name}</span>
      </div>
      <div className="ss">
        {asks ? (
          <>
            <b className="askt">{t.state === "split" ? "Split" : "Escalated"}</b> · {said.join(" · ")} · {t.fit} stands
          </>
        ) : failed ? (
          <b className="badt">ungraded · grading failed 3 runs</b>
        ) : (
          <>
            <span className="gr">{gradeLine(t.grade, paper.weight?.weight)}</span>
            {whoseFit(t) ? ` · ${whoseFit(t)}` : ""}
          </>
        )}
      </div>
      <FitControl paperId={paper.id} t={t} revision={paper.revision} />
    </div>
  );
}

export function PaperSheet({ paper, asked, onClose }: { paper: Paper; asked: boolean; onClose: () => void }) {
  const [miss, setMiss] = useState(false);
  return (
    <Sheet open onClose={onClose} label="The paper">
      {paper.in_collection && paper.topics.length > 0 && (
        <>
          <div className="cap">Label</div>
          {paper.topics.map((t) => (
            <Slab key={t.topic.slug} paper={paper} t={t} />
          ))}
        </>
      )}
      <div className="cap">The paper</div>
      <div className="agrid">
        {linkCells(paper, ["arxiv", "pdf", "latex", "code", "weights", "bib"])}
        <DeepReadCell paper={paper} asked={asked} wide />
        {!paper.in_collection &&
          (miss ? (
            <div className="wide contents">
              <MissPicker paper={paper} onDone={() => setMiss(false)} />
            </div>
          ) : (
            <button className="wide" onClick={() => setMiss(true)}>
              Report as a miss
            </button>
          ))}
      </div>
      <div className="kv sm">
        <span>Versions</span>
        <div className="vers">
          {paper.versions?.length ? paper.versions.map((v) => <span key={v.version}>v{v.version}</span>) : <span>v{paper.version ?? 1}</span>}
        </div>
        {paper.stored !== false && (
          <>
            <span>Published</span>
            <Published paper={paper} />
          </>
        )}
        {paper.in_collection && (
          <>
            <span>Classes</span>
            <Classes paper={paper} />
          </>
        )}
      </div>
      <button className="done" onClick={onClose}>
        Done
      </button>
    </Sheet>
  );
}

export function PhoneBar({ paper, under, asked, sheet, setSheet }: { paper: Paper; under: string | undefined; asked: boolean; sheet: "more" | "up" | "down" | null; setSheet: (s: "more" | "up" | "down" | null) => void }) {
  const save = useSave(paper.id);
  const deep = useDeepRead(paper.id);
  const listKey = useLocation({ select: (l) => l.state.list });
  const place = placeOf(paper.id, listKey);
  // The thumbs light for the topic the sheet opens on (sheetTopic): any topic's when the sheet would ask which.
  const on = sheetTopic(paper, under);
  const lit = (v: ReactionValue) => (on ? on.reaction?.value === v : paper.topics.length ? paper.topics.some((t) => t.reaction?.value === v) : paper.reaction?.value === v);
  return (
    <Fill name="foot">
      <div className="pfoot pgf">
        {paper.in_collection ? (
          <>
            <button className={paper.saved ? "on" : ""} onClick={() => save.mutate(!paper.saved)} disabled={save.isPending} aria-pressed={paper.saved}>
              {paper.saved ? "★" : "☆"} <small>{paper.saved ? "Saved" : "Save"}</small>
            </button>
            <button className={lit("up") ? "on" : "dim"} onClick={() => setSheet("up")} aria-label="Thumbs up">
              👍
            </button>
            <span className="lbl">{place ? `${place.index}/${place.total}` : ""}</span>
            <button className={lit("down") ? "on" : "dim"} onClick={() => setSheet("down")} aria-label="Thumbs down">
              👎
            </button>
          </>
        ) : (
          <>
            <ProcessCell paper={paper} phone />
            {paper.card_requestable && (
              <button onClick={() => deep.mutate(undefined)} disabled={deep.isPending || asked || !!paper.card_pass}>
                <small>{asked || paper.card_pass ? "Queued" : paper.card ? "Write again" : "Deep read"}</small>
              </button>
            )}
            <span className="lbl" />
          </>
        )}
        <button onClick={() => setSheet("more")} aria-label="More">
          ⋯
        </button>
      </div>
      {(sheet === "up" || sheet === "down") && <ReactSheet paper={paper} value={sheet} under={under} onClose={() => setSheet(null)} />}
      {sheet === "more" && <PaperSheet paper={paper} asked={asked} onClose={() => setSheet(null)} />}
    </Fill>
  );
}
