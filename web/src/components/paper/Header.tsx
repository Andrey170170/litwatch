/** The paper's header on its rarity ground: level and badges, title, authors, the facts line (DESIGN.md, "Paper"). */
import { useState } from "react";
import { Badge } from "@/components/Badge";
import { authorsShort, fullDate } from "@/lib/format";
import { CONTRIBUTION_NAME, RARITY_NAME, rc } from "@/lib/rarity";
import { CopyMark, copyId, type Paper } from "./Actions";

export function Header({ paper }: { paper: Paper }) {
  const [all, setAll] = useState(false);
  const awaiting = (paper.states ?? []).some((s) => s.kind === "awaiting_classification");
  const authors = all ? { shown: paper.authors, more: 0 } : authorsShort(paper.authors);
  const card = paper.card?.card as { code_url?: string } | undefined;
  const versions = paper.versions ?? [];
  const version = paper.version ? `v${paper.version}${versions.length > 1 ? ` of ${versions.length}` : ""}` : null;
  return (
    <header className={`pghead${paper.rarity ? " row" : ""}`} style={{ "--rc": rc(paper.rarity) } as React.CSSProperties}>
      <div className="mark">
        {paper.rarity ? (
          <span className="lvl">{RARITY_NAME[paper.rarity]}</span>
        ) : (
          <span className={`lvl none${awaiting ? " fail" : ""}`}>{awaiting ? "Awaiting classification" : "Not in the collection"}</span>
        )}
        {paper.badges?.map((b) => (
          <Badge key={b.slug} topic={b} />
        ))}
      </div>
      <h1 className="h1">{paper.title}</h1>
      <div className="au">
        {authors.shown.join(", ")}
        {authors.more > 0 && (
          <>
            {" "}
            <button className="more" onClick={() => setAll(true)}>
              +{authors.more}
            </button>
          </>
        )}
      </div>
      <div className="facts">
        <span className="idl">
          <a className="lnk mono" href={paper.arxiv_url} target="_blank" rel="noopener">
            {paper.id} ↗
          </a>
          <button className="cp" onClick={() => void copyId(paper.id)} title="Copy the arXiv id" aria-label="Copy the arXiv id">
            <CopyMark />
          </button>
        </span>
        {paper.published && <span className="mono">{fullDate(paper.published)}</span>}
        {version && <span className="mono">{version}</span>}
        {paper.facets && <span>{CONTRIBUTION_NAME[paper.facets.contribution]}</span>}
        {paper.publication?.venue && (
          <span>
            <b>{[paper.publication.venue, paper.publication.year].filter(Boolean).join(" ")}</b>
          </span>
        )}
        {paper.facets?.releases_code &&
          (card?.code_url ? (
            <a className="lnk" href={card.code_url} target="_blank" rel="noopener">
              ⟨/⟩ code ↗
            </a>
          ) : (
            <span title="The abstract announces released code">⟨/⟩ code</span>
          ))}
        {paper.facets?.releases_weights && <span title="The abstract announces released weights">◆ weights</span>}
      </div>
    </header>
  );
}
