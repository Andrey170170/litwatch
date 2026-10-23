/** The quick actions (DESIGN.md, "The rail"): the action grid, Deep read, the miss report and the DOI form. */
import { useQuery } from "@tanstack/react-query";
import { Copy } from "lucide-react";
import { Fragment, useState } from "react";
import type { Schemas } from "@/api/client";
import { bibtexFor, copyText, useDeepRead, useMiss, useProcess, usePublication, useSave } from "@/api/mutations";
import { topicsQuery } from "@/api/queries";
import { Badge } from "@/components/Badge";
import { processing } from "@/lib/jobs";
import { toast } from "@/lib/toast";

export type Paper = Schemas["Paper"];

/** The copy mark, drawn rather than typed: ⧉ is missing from the UI fonts. */
export const CopyMark = () => <Copy size="1em" aria-hidden style={{ verticalAlign: "-0.125em" }} />;

export function copyId(id: string) {
  return copyText(id, `Copied ${id}`);
}
export async function copyBibtex(paper: Paper) {
  try {
    await copyText(await bibtexFor(paper), "BibTeX copied");
  } catch {
    toast("Could not build the BibTeX", "error");
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

/** Deep read asks for a card, Write again a new one over the paper's own; while the request is in, or the owner's
 * own pass is open, the cell says so (the hook remembers a request for the page until the pass is read back). An
 * automatic pass leaves the cell live: the owner's request takes it over (`card_requestable`). */
export function DeepReadCell({ paper, asked, wide }: { paper: Paper; asked: boolean; wide?: boolean }) {
  const deep = useDeepRead(paper.id);
  if (!paper.card_requestable && !paper.card_pass) return null;
  const busy = asked || !paper.card_requestable;
  const what = paper.card ? "Write again" : "Deep read";
  return (
    <button className={wide ? "wide" : ""} onClick={() => deep.mutate(undefined)} disabled={deep.isPending || busy}>
      {busy ? `${what} · ${passWord(paper, asked)}` : what}
    </button>
  );
}

/** Process, for a paper outside the collection: it fetches the paper and starts the job that processes it now
 * (Jobs), and reads "Processing" while the job runs. A failed job can be asked again while the API says it would run
 * (`again`); while a step waits on the owner it reads "Failed": the open state above has the actions, and they put
 * the job back. A paper fetched otherwise (a miss report, a lookup) gets Process while the API says it would queue the
 * job now (`process_now`, false with the schedule off); otherwise it says it is fetched, and that the next run
 * processes it only while one will (`next_run`): a dismissed paper, say, is fetched and never processed. */
export function ProcessCell({ paper, phone }: { paper: Paper; phone?: boolean }) {
  const process = useProcess();
  const busy = processing(paper.process);
  const failed = paper.process?.state === "failed";
  const stuck = failed && !paper.process?.again;
  const fetched = paper.stored !== false && !busy && !failed && !paper.process_now;
  const waits = fetched && paper.next_run;
  const why = paper.process?.error ?? "no reason given";
  const label = busy ? "Processing" : stuck ? "Failed" : failed ? "Process again" : fetched ? (waits && !phone ? "Fetched · next run" : "Fetched") : "Process";
  return (
    <button
      className={phone ? (fetched || busy || stuck ? "" : "on") : "pri"}
      onClick={() => process.mutate(paper.id)}
      disabled={fetched || busy || stuck || process.isPending}
      title={stuck ? `Processing failed and waits on you: ${why}` : failed ? `Processing failed: ${why}` : waits ? "Fetched; the next run processes it" : undefined}
    >
      {phone ? <small>{label}</small> : label}
    </button>
  );
}

/** Where the owner's own pass stands, for a control: queued until it runs or fails, and while the request is in. */
export const passWord = (paper: Paper, asked: boolean) => (!asked && paper.card_pass?.state === "failed" ? "failed" : asked || paper.card_pass?.state !== "running" ? "queued" : "being written");

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
    bib: <button onClick={() => void copyBibtex(paper)}>BibTeX <CopyMark /></button>,
    id: <button onClick={() => void copyId(paper.id)}>arXiv id <CopyMark /></button>,
  };
  return keys.map((k) => cells[k] && <Fragment key={k}>{cells[k]}</Fragment>);
}

/** Report as a miss: the Focus topic (the API takes no other), and whether it is central or related, asked at once
 * (feedback.md). */
export function MissPicker({ paper, onDone }: { paper: Pick<Paper, "id">; onDone: () => void }) {
  const { data } = useQuery(topicsQuery);
  const miss = useMiss(paper.id);
  const [topic, setTopic] = useState<string | null>(null);
  const topics = data?.find((g) => g.depth === "focus")?.topics ?? [];
  if (!topics.length) return <div className="why">No Focus topic is followed.</div>;
  return (
    <div className="why" role="group" aria-label="Report as a miss">
      {!topic
        ? topics.map((t) => (
            <button key={t.slug} onClick={() => setTopic(t.slug)}>
              <Badge topic={t} size="sm" /> {t.name}
            </button>
          ))
        : (["central", "related"] as const).map((fit) => (
            <button key={fit} onClick={() => miss.mutate({ topic, fit }, { onSuccess: onDone })} disabled={miss.isPending}>
              {fit} on {topics.find((t) => t.slug === topic)?.name}
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
