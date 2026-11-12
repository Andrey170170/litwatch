import { Diagram } from "./Diagram";
import { Figure } from "./Figure";
import { Inline } from "./inline";
import { Tex } from "./math";
import { Table } from "./Table";
import type { Block, BlockOf } from "./types";

/** An experiment, the unit of a finding: its question, then what was done, found, what it means and what it leaves open. */
function Experiment({ block }: { block: BlockOf<"xp"> }) {
  const rows: [string, string, string | undefined][] = [
    ["did", "Did", block.did],
    ["got", "Found", block.found],
    ["", "Means", block.means],
    ["not", "Not shown", block.not_shown],
  ];
  return (
    <div className="dr-xp">
      <div className="xq">
        {block.label && <span className="n">{block.label}</span>}
        <span>
          <Inline text={block.question} />
        </span>
      </div>
      <div className="xr">
        {rows.map(
          ([cls, name, text]) =>
            text && (
              <div key={name} className="x">
                <span className={cls || undefined}>{name}</span>
                <div>
                  <Inline text={text} />
                </div>
              </div>
            ),
        )}
      </div>
    </div>
  );
}

/** One block of a section, by its type (`deep/src/read.ts`). */
export function BlockView({ block }: { block: Block }) {
  switch (block.type) {
    case "p":
      return (
        <p className="dr-p">
          <Inline text={block.text} />
        </p>
      );
    case "h3":
      return <h3 className="dr-h3">{block.text}</h3>;
    case "list":
      return (
        <ul className="dr-ul">
          {block.items.map((item, i) => (
            <li key={i}>
              <Inline text={item} />
            </li>
          ))}
        </ul>
      );
    case "why":
      // folded, its question in view; a read from before P6 has no question and shows the box open
      return block.question ? (
        <details className="dr-why">
          <summary>{block.question}</summary>
          <Inline text={block.text} />
        </details>
      ) : (
        <div className="dr-why">
          <Inline text={block.text} />
        </div>
      );
    case "note":
      return (
        <div className="dr-note">
          <Inline text={block.text} /> <span className="src">· {block.where}</span>
        </div>
      );
    case "eq":
      return (
        <div className="dr-eq">
          <Tex tex={block.tex} display />
          {block.caption && (
            <div className="en">
              <Inline text={block.caption} />
            </div>
          )}
        </div>
      );
    case "fig":
      return <Figure block={block} />;
    case "xp":
      return <Experiment block={block} />;
    case "table":
      return <Table block={block} />;
    case "diagram":
      return <Diagram block={block} />;
  }
}
