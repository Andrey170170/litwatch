/**
 * A Deep read on the paper page (docs/specs/deep-read.md; sketch R2, variant C): under a "Deep read" rung, each
 * section is a rung of the ladder, its name in the side column with its colour square, its body beside it, all laid
 * out in full. Only a claim's evidence and the process notes are folded.
 *
 * Every section rung has the id `dr-<section id>` and `data-section`; every block sits in an element with
 * `data-block="<section id>.<its index in the section>"`. The render page's screenshots target those.
 */
import type { ReactNode } from "react";
import type { Schemas } from "@/api/client";
import { ProcessNotes } from "@/components/paper/ProcessNotes";
import { Rung } from "@/components/paper/Rung";
import { useDesktop } from "@/hooks/useDesktop";
import { rc } from "@/lib/rarity";
import { BlockView } from "./blocks";
import { Claims } from "./Claims";
import { Chips, sectionId } from "./contents";
import { ReadEnvProvider, useReadEnv, type ReadEnv } from "./env";
import { Inline } from "./inline";
import { sectionMark, type Fact, type Read, type Section, type SectionOf } from "./types";

type Among = Schemas["AmongRow"];

function Facts({ facts }: { facts: Fact[] }) {
  return (
    <div className="dr-facts">
      {facts.map((f, i) => (
        <div key={i} className="f">
          <span>{f.name}</span>
          <div>
            {f.text && <Inline text={f.text} />}
            {f.tags && (
              <span className="tags">
                {f.tags.map((t, k) => (
                  <i key={k} className={`dr-tag${t.state ? ` ${t.state}` : ""}`}>
                    {t.text}
                    {t.state && <span aria-label={t.state === "yes" ? "released" : "not released"}> {t.state === "yes" ? "✓" : "✗"}</span>}
                  </i>
                ))}
              </span>
            )}
          </div>
        </div>
      ))}
    </div>
  );
}

/** Related work: the closest prior work, then the collection's papers the read places this one among, each a link
 * to that paper's page where it has an arXiv id. */
function Related({ section, among }: { section: SectionOf<"related">; among: Among[] }) {
  const { PaperLink } = useReadEnv();
  return (
    <>
      {section.prior_work.map((p, i) =>
        p.arxiv_id ? (
          <PaperLink key={i} id={p.arxiv_id} className="pw">
            <span className="ti">{p.title}</span>
            <span className="rel">{p.relation}</span>
            <span className="mono">{p.arxiv_id}</span>
          </PaperLink>
        ) : (
          <div key={i} className="pw nolink">
            <span className="ti">{p.title}</span>
            <span className="rel">{p.relation}</span>
            <span className="faint">no arXiv id</span>
          </div>
        ),
      )}
      {among.length > 0 && <div className="dr-sub">Among the collection</div>}
      {among.map((a) => (
        <PaperLink key={a.id} id={a.id} className="pw row among" style={{ "--rc": rc(a.rarity) } as React.CSSProperties}>
          <span className="ti">{a.title}</span>
          <span className="rel">
            <i className="tag">{a.relation}</i>
            {a.note}
          </span>
          <span className="mono">{a.in_collection ? a.id : "outside the collection"}</span>
        </PaperLink>
      ))}
    </>
  );
}

function SectionBody({ section, among }: { section: Section; among: Among[] }) {
  switch (section.kind) {
    case "glance":
      return (
        <>
          <p className="dr-claim">
            <Inline text={section.claim} />
          </p>
          <ul className="dr-pts">
            {section.points.map((p, i) => (
              <li key={i}>
                <Inline text={p} />
              </li>
            ))}
          </ul>
          <Facts facts={section.facts} />
        </>
      );
    case "claims":
      return <Claims claims={section.claims} />;
    case "limits":
      return (
        <ul className="dr-lim">
          {section.limitations.map((l, i) => (
            <li key={i}>
              <span className={`who ${l.by}`}>{l.by}</span>
              <span>
                <Inline text={l.text} />
              </span>
            </li>
          ))}
        </ul>
      );
    case "related":
      return <Related section={section} among={among} />;
    case "repro":
      return <Facts facts={section.facts} />;
    default:
      return section.blocks.map((b, i) => (
        <div key={i} className="dr-b" data-block={`${section.id}.${i}`}>
          <BlockView block={b} />
        </div>
      ));
  }
}

type Props = {
  read: Read;
  /** The read's `among_collection` as the page knows those papers (the API's rows). */
  among: Among[];
  env: ReadEnv;
  /** What closes the read: in the app, what it was written from and Write again. */
  children?: ReactNode;
};

export function DeepRead({ read, among, env, children }: Props) {
  const desktop = useDesktop();
  return (
    <ReadEnvProvider value={env}>
      <div className="dread">
        <Rung name="Deep read" className="dr-head">
          {null}
        </Rung>
        {!desktop && <Chips sections={read.sections} topicColour={env.topicColour} />}
        {read.sections.map((s) => (
          <Rung key={s.id} name={s.title} className="dr-sec" id={sectionId(s.id)} data-section={s.id} tabIndex={-1} {...sectionMark(s, env.topicColour)}>
            <SectionBody section={s} among={among} />
          </Rung>
        ))}
        {read.process_notes?.length ? <ProcessNotes notes={read.process_notes} /> : null}
        {children}
      </div>
    </ReadEnvProvider>
  );
}
