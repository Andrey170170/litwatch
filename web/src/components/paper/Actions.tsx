/** The quick actions (DESIGN.md, "The rail"): the action grid, Deep read, the miss report and the DOI form. */
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import type { Schemas } from "@/api/client";
import { bibtexFor, copyText, useDeepRead, useMiss, usePublication, useSave } from "@/api/mutations";
import { topicsQuery } from "@/api/queries";
import { Badge } from "@/components/Badge";

export type Paper = Schemas["Paper"];

export function copyId(id: string) {
  return copyText(id, `Copied ${id}`);
}
export async function copyBibtex(id: string) {
  try {
    await copyText(await bibtexFor(id), "BibTeX copied");
  } catch {
    copyText("", "Could not build the BibTeX");
  }
}

export function SaveCell({ paper }: { paper: Paper }) {
  const save = useSave(paper.id);
  return (
    <button className={paper.saved ? "on" : ""} onClick={() => save.mutate(!paper.saved)} disabled={save.isPending} aria-pressed={paper.saved}>
      {paper.saved ? "★ Saved" : "☆ Save"}
    </button>
  );
}

/** Deep read asks for a card; while the request is in, the cell says so. */
export function DeepReadCell({ paper, asked, onAsked, wide }: { paper: Paper; asked: boolean; onAsked: () => void; wide?: boolean }) {
  const deep = useDeepRead(paper.id);
  if (paper.card || !paper.card_requestable) return null;
  return (
    <button className={wide ? "wide" : ""} onClick={() => deep.mutate(undefined, { onSuccess: onAsked })} disabled={deep.isPending || asked}>
      {asked ? "Deep read · queued" : "Deep read"}
    </button>
  );
}

const ext = (href: string, label: string) => (
  <a href={href} target="_blank" rel="noopener">
    {label} ↗
  </a>
);

/** The links as cells: arXiv, PDF, LaTeX, Code and Weights where the card knows them, BibTeX. */
export function linkCells(paper: Paper, keys: ("arxiv" | "pdf" | "latex" | "code" | "weights" | "bib" | "id")[]) {
  const card = paper.card?.card as { code_url?: string } | undefined;
  const cells: Record<string, React.ReactNode> = {
    arxiv: ext(paper.arxiv_url, "arXiv"),
    pdf: ext(paper.pdf_url, "PDF"),
    latex: ext(paper.source_url, "LaTeX"),
    code: paper.facets?.releases_code ? (card?.code_url ? ext(card.code_url, "Code") : <span title="The abstract announces code; no link is known yet">⟨/⟩ Code</span>) : null,
    weights: paper.facets?.releases_weights ? <span title="The abstract announces weights; no link is known yet">◆ Weights</span> : null,
    bib: <button onClick={() => void copyBibtex(paper.id)}>BibTeX ⧉</button>,
    id: <button onClick={() => void copyId(paper.id)}>arXiv id ⧉</button>,
  };
  return keys.map((k) => cells[k] && <span key={k} className="contents">{cells[k]}</span>);
}

/** Report as a miss: the Focus topic, and whether it is central or related, asked at once (feedback.md). */
export function MissPicker({ paper, onDone }: { paper: Paper; onDone: () => void }) {
  const { data } = useQuery(topicsQuery);
  const miss = useMiss(paper.id);
  const [topic, setTopic] = useState<string | null>(null);
  const focus = data?.find((g) => g.depth === "focus")?.topics ?? [];
  if (!focus.length) return <div className="why">No Focus topic is followed.</div>;
  return (
    <div className="why" role="group" aria-label="Report as a miss">
      {!topic
        ? focus.map((t) => (
            <button key={t.slug} onClick={() => setTopic(t.slug)}>
              <Badge topic={t} size="sm" /> {t.name}
            </button>
          ))
        : (["central", "related"] as const).map((fit) => (
            <button key={fit} onClick={() => miss.mutate({ topic, fit }, { onSuccess: onDone })} disabled={miss.isPending}>
              {fit} on {focus.find((t) => t.slug === topic)?.name}
            </button>
          ))}
      <button className="clr" onClick={onDone}>
        Cancel
      </button>
    </div>
  );
}

/** "Paste a DOI": the owner names the published version. */
export function DoiForm({ paper, onDone }: { paper: Paper; onDone: () => void }) {
  const publication = usePublication(paper.id);
  const [doi, setDoi] = useState("");
  return (
    <form
      className="srow mt-1.5"
      onSubmit={(e) => {
        e.preventDefault();
        if (doi.trim()) publication.mutate({ doi: doi.trim() }, { onSuccess: onDone });
      }}
    >
      <input className="field" style={{ maxWidth: 260 }} value={doi} onChange={(e) => setDoi(e.target.value)} placeholder="10.1234/…" aria-label="DOI" autoFocus />
      <button className="btn" type="submit" disabled={!doi.trim() || publication.isPending}>
        Set
      </button>
      <button className="btn" type="button" onClick={onDone}>
        Cancel
      </button>
    </form>
  );
}
