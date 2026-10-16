/** The ladder (DESIGN.md, "The ladder"): Gist, the Brief or the abstract standing in for it, the Deep card, the paper. */
import { useEffect, useRef, useState, type ReactNode } from "react";
import { fullDate } from "@/lib/format";
import { useDesktop } from "@/hooks/useDesktop";
import { DoiForm, type Paper } from "./Actions";
import { DeepCard } from "./DeepCard";

export function Rung({ name, extra, className, children }: { name: string; extra?: ReactNode; className?: string; children: ReactNode }) {
  return (
    <section className={`rung${className ? ` ${className}` : ""}`}>
      <div className="rl">
        {name}
        {extra}
      </div>
      <div className="rb">{children}</div>
    </section>
  );
}

/** The paper's classes with their parents, members only. */
export function Classes({ paper }: { paper: Paper }) {
  const members = paper.classification?.classes.filter((c) => c.member) ?? [];
  if (!members.length) return <span className="faint">none</span>;
  return (
    <div className="vers">
      {members.map((c) => (
        <span key={c.slug}>{c.path.join(" › ")}</span>
      ))}
    </div>
  );
}

export function Published({ paper }: { paper: Paper }) {
  const [doi, setDoi] = useState(false);
  const p = paper.publication;
  if (p && (p.venue || p.doi))
    return (
      <div>
        {[p.venue, p.year].filter(Boolean).join(" ") || p.doi}
        {p.doi && (
          <>
            {" "}
            <a className="lnk" href={`https://doi.org/${p.doi}`} target="_blank" rel="noopener">
              doi ↗
            </a>
          </>
        )}
      </div>
    );
  return (
    <div>
      <span className="faint">{paper.publication_by_owner ? "No published version." : "Preprint only, as far as DBLP knows."}</span>{" "}
      {!doi && (
        <button className="inl" onClick={() => setDoi(true)}>
          Paste a DOI
        </button>
      )}
      {doi && <DoiForm paper={paper} onDone={() => setDoi(false)} />}
    </div>
  );
}

export function ThePaper({ paper }: { paper: Paper }) {
  const ext = (href: string, label: string) => (
    <a className="lnk" href={href} target="_blank" rel="noopener">
      {label} ↗
    </a>
  );
  return (
    <Rung name="The paper">
      <div className="links">
        {ext(paper.arxiv_url, "arXiv page")}
        {ext(paper.pdf_url, "PDF")}
        {ext(paper.source_url, "LaTeX source")}
      </div>
      <div className="kv sm">
        <span>Versions</span>
        <div className="vers">
          {paper.versions?.length ? (
            paper.versions.map((v) => (
              <span key={v.version}>
                v{v.version}
                {v.date ? ` · ${fullDate(v.date)}` : ""}
                {v.version === paper.version ? <span className="faint"> (current)</span> : ""}
              </span>
            ))
          ) : (
            <span className="faint">v{paper.version ?? 1}</span>
          )}
        </div>
        {/* A paper litwatch has not fetched has no publication lookup, and a DOI cannot be set on it yet. */}
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
    </Rung>
  );
}

type Props = { paper: Paper; asked: boolean; deepRead: ReactNode; onGistAway: (away: boolean) => void };

/** The quiet line over the abstract when it stands in for the Brief: where the Brief is, or why there is none. A
 * pass waiting on the owner has its note above, with Run it now. */
export function whyAbstract(paper: Paper): string {
  const stuck = (paper.states ?? []).some((s) => s.kind === "fulltext_failed" || s.kind === "awaiting_text");
  switch (paper.text_pass?.state) {
    case "awaiting_text":
      return stuck ? "The Brief has waited for the paper's full text over a week; the abstract stands in." : "The Brief waits for the paper's full text; the abstract stands in.";
    case "ready":
    case "running":
      return "The Brief is being written from the full text; the abstract stands in.";
    case "failed":
      return stuck ? "The full-text pass failed three runs in a row; the abstract stands in until it is run again." : "The full-text pass failed; it is tried again on the next run. The abstract stands in.";
    default:
      return "No Brief yet: Briefs are written for Focus papers at central or related and for Area must reads, so the abstract stands in.";
  }
}

type Brief = NonNullable<Paper["brief"]>;

/** The Brief's text: the overstatement first where the full text does not support the abstract, and which revision
 * of the text it read when the paper has moved on since. The ladder and Calibrate's blind paper share it. */
export function BriefBody({ paper, brief }: { paper: Paper; brief: Brief }) {
  const older = brief.revision < paper.revision;
  return (
    <>
      {brief.overstatement && <div className="overst">{brief.overstatement}</div>}
      {older && (
        <div className="why0">
          Written for revision {brief.revision} of the text{brief.arxiv_version ? ` (arXiv v${brief.arxiv_version})` : ""}; the current one, revision {paper.revision}, waits for its full text.
        </div>
      )}
      <p className="prose">{brief.text}</p>
    </>
  );
}

function BriefRung({ paper, brief }: { paper: Paper; brief: Brief }) {
  return (
    <Rung name="Brief" extra={<span className="faint normal-case tracking-normal">{brief.source === "latex" ? "from the LaTeX" : "from the PDF"}</span>}>
      <BriefBody paper={paper} brief={brief} />
    </Rung>
  );
}

export function Ladder({ paper, asked, deepRead, onGistAway }: Props) {
  const desktop = useDesktop();
  const gistRef = useRef<HTMLDivElement>(null);
  const analysisFailed = (paper.states ?? []).some((s) => s.kind === "analysis_stopped");
  useEffect(() => {
    const el = gistRef.current;
    if (!el || typeof IntersectionObserver === "undefined") return;
    const io = new IntersectionObserver(([e]) => e && onGistAway(!e.isIntersecting && e.boundingClientRect.top < 0));
    io.observe(el);
    return () => io.disconnect();
  }, [onGistAway, paper.id]);
  return (
    <div className="ladder">
      {paper.gist && !analysisFailed && (
        <div ref={gistRef}>
          <Rung name="Gist" extra={paper.gist.source === "full_text" ? <span className="faint normal-case tracking-normal">from the full text</span> : undefined}>
            <p className="prose gist">{paper.gist.text}</p>
          </Rung>
        </div>
      )}
      {paper.brief && !analysisFailed ? (
        <BriefRung paper={paper} brief={paper.brief} />
      ) : (
        <Rung name="Abstract">
          {paper.in_collection && !analysisFailed && <div className="why0">{whyAbstract(paper)}</div>}
          <p className="prose">{paper.abstract}</p>
        </Rung>
      )}
      {(paper.in_collection || paper.card || paper.card_pass || asked) && <DeepCard paper={paper} asked={asked} deepRead={deepRead} />}
      {desktop && <ThePaper paper={paper} />}
    </div>
  );
}
