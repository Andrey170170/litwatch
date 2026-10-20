/** The Deep card rung (DESIGN.md, "The ladder", 3): the claim, then each section as a rung of its own under it. */
import { Link } from "@tanstack/react-router";
import { useState } from "react";
import type { Schemas } from "@/api/client";
import { useDeepRead } from "@/api/mutations";
import { dateTime } from "@/lib/format";
import { rc } from "@/lib/rarity";
import type { ProcessNote } from "@/lib/settings";
import type { Paper } from "./Actions";
import { Rung } from "./Ladder";

type CardPass = Schemas["CardPass"];
const WHY: Record<CardPass["reason"], string> = { request: "Asked for", focus: "Automatic for a Focus must read", pick: "Automatic for an Area pick" };
const DOING: Record<CardPass["state"], string> = { awaiting_text: "waits for the full text", ready: "queued", running: "being written", failed: "failed; it tries again on the next run" };

/** The status line while a card is queued or being written (`card_pass`), or just asked for and not yet read back
 * (`asked`): why it is coming and where it stands, with a pulsing dot. A pass failed for good is the note above
 * the ladder; here it is one line. */
export function CardStatus({ pass, asked, again }: { pass: CardPass | null | undefined; asked: boolean; again?: boolean }) {
  if (!pass) return asked ? <div className="cardwait"><span className="dot" />Asked for · queued · the card shows here once it is written</div> : null;
  const stuck = pass.state === "failed" && pass.failed_runs >= 3;
  return (
    <div className={`cardwait${stuck ? " stuck" : ""}`}>
      {!stuck && <span className="dot" />}
      {again ? "Writing again" : WHY[pass.reason]} · {stuck ? "failed three runs in a row" : DOING[pass.state]}
      {pass.state === "failed" && !stuck ? ` (${pass.failed_runs} of 3)` : ""}
    </div>
  );
}

const RELATION: Record<Schemas["AmongRow"]["relation"], string> = { agrees: "agrees", contradicts: "contradicts", extends: "extends", supersedes: "supersedes" };

/** What the card was written from and when: "Written from the LaTeX source of arXiv v2 (text revision 3), 14 Oct,
 * 03:12, by model". */
function label(card: Schemas["DeepCard"]): string {
  const from = card.source ? `from the ${card.source === "latex" ? "LaTeX source" : "PDF"}` : "";
  const version = card.arxiv_version ? ` of arXiv v${card.arxiv_version}` : "";
  const revision = card.revision ? ` (text revision ${card.revision})` : "";
  const when = card.written_at ? `, ${dateTime(card.written_at)}` : "";
  const by = card.model ? `, by ${card.model}` : "";
  return `Written ${from}${version}${revision}${when}${by}`.replace("Written  ", "Written ").replace("Written ,", "Written");
}

type Setup = { models?: string[]; tasks?: string[]; generalization?: string };
type Prior = { title: string; relation: string; arxiv_id?: string };
type Card = {
  key_claim?: string;
  summary?: string;
  evaluation_setup?: Setup;
  limitations?: string[];
  prior_work?: Prior[];
  process_notes?: ProcessNote[];
  [extension: string]: unknown;
};
const CORE = new Set(["key_claim", "summary", "paper_kind", "evaluation_setup", "limitations", "process_notes", "prior_work", "code_available", "code_url", "weights_available"]);
const EXTENSION_NAME: Record<string, string> = { faithfulness: "CoT faithfulness", monitoring: "CoT monitoring" };
const pretty = (k: string) => k.replace(/_/g, " ").replace(/^\w/, (c) => c.toUpperCase());

const Tags = ({ items }: { items: string[] }) => (
  <div>
    {items.map((m) => (
      <i key={m} className="tag">
        {m}
      </i>
    ))}
  </div>
);

/** A topic's own section: enum facts as tags, then its text, in the order the card gives them. */
function Extension({ value }: { value: Record<string, unknown> }) {
  const entries = Object.entries(value);
  const facts = entries.filter(([, v]) => typeof v !== "string" || v.length < 40);
  const text = entries.filter(([, v]) => typeof v === "string" && v.length >= 40);
  return (
    <div className="kv">
      {[...facts, ...text].map(([k, v]) => (
        <Fact key={k} name={pretty(k)} value={v} />
      ))}
    </div>
  );
}
function Fact({ name, value }: { name: string; value: unknown }) {
  const items = Array.isArray(value) ? value.map(String) : typeof value === "string" && value.length < 40 ? [value] : null;
  return (
    <>
      <span>{name}</span>
      {items ? <Tags items={items.length ? items : ["none"]} /> : <div>{String(value)}</div>}
    </>
  );
}

/** A URL to link, only when it is http(s): a note's URL comes from the model, and is never a `javascript:` link. */
export function webUrl(url: string | undefined): URL | null {
  if (!url) return null;
  try {
    const u = new URL(url);
    return u.protocol === "https:" || u.protocol === "http:" ? u : null;
  } catch {
    return null;
  }
}

/** What Deep's own process held back (GLOSSARY.md, process note): folded under "Process notes · N" until opened, each
 * note with its kind as a tag and its page as a link. It starts folded on every card. */
function ProcessNotes({ notes }: { notes: ProcessNote[] }) {
  const [open, setOpen] = useState(false);
  return (
    <Rung name="" className="sub pnotes">
      <button className="tpfold" onClick={() => setOpen(!open)} aria-expanded={open}>
        <span className={`car${open ? " open" : ""}`} aria-hidden />
        <span className="ct">
          Process notes · <b>{notes.length}</b>
        </span>
      </button>
      {open && (
        <ul className="lim">
          {notes.map((n, i) => {
            const link = webUrl(n.url);
            return (
              <li key={i}>
                <i className="tag">{n.kind}</i>
                {n.note}
                {link ? (
                  <>
                    {" "}
                    <a className="lnk" href={link.href} target="_blank" rel="noopener noreferrer" title={link.href}>
                      {link.hostname} ↗
                    </a>
                  </>
                ) : n.url ? (
                  <span className="faint"> {n.url}</span>
                ) : null}
              </li>
            );
          })}
        </ul>
      )}
    </Rung>
  );
}

export function DeepCard({ paper, asked, deepRead }: { paper: Paper; asked: boolean; deepRead: React.ReactNode }) {
  const again = useDeepRead(paper.id);
  if (!paper.card) {
    if (asked || paper.card_pass)
      return (
        <Rung name="Deep card">
          <CardStatus pass={paper.card_pass} asked={asked} />
        </Rung>
      );
    return (
      <Rung name="Deep card">
        <p className="prose faint">
          None: cards are automatic for Focus must reads and Area picks. {deepRead}
        </p>
      </Rung>
    );
  }
  const c = paper.card.card as Card;
  const setup = c.evaluation_setup;
  const extensions = Object.entries(c).filter(([k, v]) => !CORE.has(k) && v && typeof v === "object" && !Array.isArray(v));
  return (
    <>
      <Rung name="Deep card" className="card">
        {c.key_claim && <p className="claim">{c.key_claim}</p>}
      </Rung>
      {c.summary && (
        <Rung name="Summary" className="sub">
          {c.summary.split(/\n\s*\n/).map((p, i) => (
            <p key={i} className="prose">
              {p}
            </p>
          ))}
        </Rung>
      )}
      {setup && (
        <Rung name="Setup" className="sub">
          <div className="kv">
            {setup.models && (
              <>
                <span>Models</span>
                <Tags items={setup.models} />
              </>
            )}
            {setup.tasks && (
              <>
                <span>Tasks</span>
                <Tags items={setup.tasks} />
              </>
            )}
            {setup.generalization && (
              <>
                <span>Transfer</span>
                <div>{setup.generalization}</div>
              </>
            )}
          </div>
        </Rung>
      )}
      {extensions.map(([k, v]) => (
        <Rung key={k} name={EXTENSION_NAME[k] ?? pretty(k)} className="sub">
          <Extension value={v as Record<string, unknown>} />
        </Rung>
      ))}
      {c.limitations?.length ? (
        <Rung name="Limitations" className="sub">
          <ul className="lim">
            {c.limitations.map((l, i) => (
              <li key={i}>{l}</li>
            ))}
          </ul>
        </Rung>
      ) : null}
      {/* keyed by the card, so a card written again starts folded */}
      {c.process_notes?.length ? <ProcessNotes key={`${paper.id}:${paper.card.written_at}`} notes={c.process_notes} /> : null}
      {c.prior_work?.length ? (
        <Rung name="Prior work" className="sub">
          {c.prior_work.map((p, i) =>
            p.arxiv_id ? (
              <Link key={i} to="/paper/$" params={{ _splat: p.arxiv_id }} className="pw">
                <span className="ti">{p.title}</span>
                <span className="rel">{p.relation}</span>
                <span className="mono">{p.arxiv_id}</span>
              </Link>
            ) : (
              <div key={i} className="pw nolink">
                <span className="ti">{p.title}</span>
                <span className="rel">{p.relation}</span>
                <span className="faint">no arXiv id</span>
              </div>
            ),
          )}
        </Rung>
      ) : null}
      {paper.card.among?.length ? (
        <Rung name="Among the collection" className="sub">
          {paper.card.among.map((a) => (
            <Link key={a.id} to="/paper/$" params={{ _splat: a.id }} className="pw row among" style={{ "--rc": rc(a.rarity) } as React.CSSProperties}>
              <span className="ti">{a.title}</span>
              <span className="rel">
                <i className="tag">{RELATION[a.relation]}</i>
                {a.note}
              </span>
              <span className="mono">{a.in_collection ? a.id : "outside the collection"}</span>
            </Link>
          ))}
        </Rung>
      ) : null}
      <Rung name="" className="sub end">
        <div className="cfoot">
          {label(paper.card)}
          {paper.card_requestable && !asked && (
            <>
              {" · "}
              <button className="inl" onClick={() => again.mutate(undefined)} disabled={again.isPending}>
                Write again
              </button>
            </>
          )}
        </div>
        {(asked || paper.card_pass) && <CardStatus pass={paper.card_pass} asked={asked} again />}
      </Rung>
    </>
  );
}
