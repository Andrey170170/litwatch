/** The ladder (DESIGN.md, "The ladder"): Gist, Abstract standing in for the Brief, the Deep card, the paper. */
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
        <span>Published</span>
        <Published paper={paper} />
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
          <Rung name="Gist">
            <p className="prose gist">{paper.gist.text}</p>
          </Rung>
        </div>
      )}
      <Rung name="Abstract">
        {paper.in_collection && !analysisFailed && <div className="why0">No Brief yet: Briefs are written for Focus papers at central or related and for Area must reads, so the abstract stands in.</div>}
        <p className="prose">{paper.abstract}</p>
      </Rung>
      {paper.in_collection && <DeepCard paper={paper} asked={asked} deepRead={deepRead} />}
      {desktop && <ThePaper paper={paper} />}
    </div>
  );
}
