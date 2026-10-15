/** The Deep card rung (DESIGN.md, "The ladder", 3): the claim, then each section as a rung of its own under it. */
import { Link } from "@tanstack/react-router";
import { dateTime } from "@/lib/format";
import type { Paper } from "./Actions";
import { Rung } from "./Ladder";

type Setup = { models?: string[]; tasks?: string[]; generalization?: string };
type Prior = { title: string; relation: string; arxiv_id?: string };
type Card = {
  key_claim?: string;
  summary?: string;
  evaluation_setup?: Setup;
  limitations?: string[];
  prior_work?: Prior[];
  [extension: string]: unknown;
};
const CORE = new Set(["key_claim", "summary", "paper_kind", "evaluation_setup", "limitations", "prior_work", "code_available", "code_url", "weights_available"]);
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

export function DeepCard({ paper, asked, deepRead }: { paper: Paper; asked: boolean; deepRead: React.ReactNode }) {
  if (!paper.card) {
    if (asked)
      return (
        <Rung name="Deep card">
          <div className="cardwait">
            <span className="dot" />
            Queued · the card shows here once it is written
          </div>
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
      <Rung name="" className="sub end">
        <div className="cfoot">
          Written {paper.card.written_at ? dateTime(paper.card.written_at) : ""}
          {paper.card.model ? ` by ${paper.card.model}` : ""}
        </div>
      </Rung>
    </>
  );
}
