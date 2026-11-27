/** The quick actions (DESIGN.md, "The rail"): the action grid, Deep read, the miss report and the DOI form. */
import { useQuery } from "@tanstack/react-query";
import { Copy } from "lucide-react";
import { Fragment, useState } from "react";
import type { Schemas } from "@/api/client";
import { bibtexFor, copyText, useDeepRead, useFindLinks, useMiss, useProcess, usePublication, useSave } from "@/api/mutations";
import { topicsQuery } from "@/api/queries";
import { Badge } from "@/components/Badge";
import { fullDate } from "@/lib/format";
import { processing } from "@/lib/jobs";
import { toast } from "@/lib/toast";

export type Paper = Schemas["Paper"];
export type ReleaseLink = Schemas["ReleaseLink"];

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

const KIND_MARK = { code: "⟨/⟩", weights: "◆" } as const;
const STATE_WORD = { public: "", gated: "gated", placeholder: "placeholder", missing: "missing" } as const;

/** What a release link's chip says on hover: its state, license and when it was last checked. */
const linkTitle = (l: ReleaseLink) => `${l.state}${l.license ? ` · ${l.license}` : ""} · checked ${fullDate(l.checked_at)}`;

/** A release link as a chip coloured by its access state (DESIGN.md, "Paper"): `name` the kind's word as the place
 * shows it ("code", "Code"); a state other than public is said after it. */
export function ReleaseChip({ link, name }: { link: ReleaseLink; name: string }) {
  const word = STATE_WORD[link.state];
  return (
    <a className="rlink" data-state={link.state} href={link.url} target="_blank" rel="noopener" title={linkTitle(link)}>
      {KIND_MARK[link.kind]} {name}
      {word && ` · ${word}`} ↗
    </a>
  );
}

/** Why a kind has no link: none is known yet, or the last answer for the paper as it is found none; `announced` when
 * the analysis says the paper releases it. */
export function noLinkTitle(paper: Paper, kind: "code" | "weights", announced = true) {
  const a = paper.link_answer;
  const said = announced ? `The abstract announces ${kind}; ` : "";
  const none = a ? `no link was found (${fullDate(a.at)})` : "no link is known yet";
  return said ? said + none : none.charAt(0).toUpperCase() + none.slice(1);
}

/** The links as cells: arXiv, PDF, LaTeX, Code and Weights, BibTeX. Code and Weights are always there, so a find never
 * moves a cell: the first release link of each, by state; else the Deep card's Code link; else the analysis's word,
 * or an empty quiet cell. */
export function linkCells(paper: Paper, keys: ("arxiv" | "pdf" | "latex" | "code" | "weights" | "bib" | "id")[]) {
  const card = paper.card?.card as { code_url?: string } | undefined;
  const code = paper.links?.find((l) => l.kind === "code");
  const weights = paper.links?.find((l) => l.kind === "weights");
  const cells: Record<string, React.ReactNode> = {
    arxiv: ext(paper.arxiv_url, "arXiv"),
    pdf: ext(paper.pdf_url, "PDF"),
    latex: ext(paper.source_url, "LaTeX"),
    code: code ? <ReleaseChip link={code} name="Code" /> : card?.code_url ? ext(card.code_url, "Code") : <NoLink paper={paper} kind="code" />,
    weights: weights ? <ReleaseChip link={weights} name="Weights" /> : <NoLink paper={paper} kind="weights" />,
    bib: <button onClick={() => void copyBibtex(paper)}>BibTeX <CopyMark /></button>,
    id: <button onClick={() => void copyId(paper.id)}>arXiv id <CopyMark /></button>,
  };
  return keys.map((k) => cells[k] && <Fragment key={k}>{cells[k]}</Fragment>);
}

/** A kind's cell with no link: the analysis's word where it announces the kind, else empty and quiet. */
function NoLink({ paper, kind }: { paper: Paper; kind: "code" | "weights" }) {
  const announced = kind === "code" ? paper.facets?.releases_code : paper.facets?.releases_weights;
  return (
    <span className={announced ? undefined : "quiet"} title={noLinkTitle(paper, kind, !!announced)}>
      {KIND_MARK[kind]} {kind === "code" ? "Code" : "Weights"}
    </span>
  );
}

/** What the owner's last find of the paper as it is says on its cell: queued or finding while open; then that it
 * failed, or found none; nothing once it found links, which show in their cells. */
function ownersOutcome(f: Paper["owner_find"]): string | null {
  if (!f) return null;
  if (f.state === "queued") return "queued";
  if (f.state === "running") return "finding";
  if (f.state === "failed") return "failed";
  return f.answered && f.links === 0 ? "none found" : null;
}

/** Find links asks the deep service for the paper's release links afresh; its label says how the owner's last find of
 * the paper as it is stands or ended. It comes last in its grid, so the cells before it keep their places. */
export function FindLinksCell({ paper }: { paper: Paper }) {
  const find = useFindLinks(paper.id);
  const f = paper.owner_find;
  const open = f?.state === "queued" || f?.state === "running";
  if (!paper.links_findable && !open) return null;
  const outcome = ownersOutcome(f);
  const failed = f?.state === "failed" ? `The last find failed: ${f.error ?? "no reason given"}` : undefined;
  return (
    <button className={`wide${f?.state === "failed" ? " fail" : ""}`} onClick={() => find.mutate(undefined)} disabled={find.isPending || open} title={failed}>
      {outcome ? `Find links · ${outcome}` : "Find links"}
    </button>
  );
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
