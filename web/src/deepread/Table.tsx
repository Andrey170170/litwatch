import { useEffect, useRef, useState, type CSSProperties } from "react";
import { Inline } from "./inline";
import { COLOUR, type BlockOf } from "./types";

type Row = BlockOf<"table">["rows"][number];
type Cell = Extract<Row, { cells: unknown }>["cells"][number];
const cellOf = (c: Cell): { value: string | number; note?: string } => (typeof c === "object" ? c : { value: c });
/** A full-strength heat cell is mixed this far into the page, so its number stays readable. */
const HEAT = 50;
/** Whether a cell reads as a figure: a number, or text that opens with one ("92–94%", "+0.25 / +0.78", "<1"). */
const figure = (value: string | number) => typeof value === "number" || /^[+\-−±<>≤≥≈~]?\s?\d/.test(value);
/** Which columns hold figures and so line up on the right; a column of words reads from the left. The first column
 * names the rows. */
function figureColumns(block: BlockOf<"table">): boolean[] {
  return block.columns.map((_, i) => {
    const values = block.rows.flatMap((r) => ("cells" in r && i < r.cells.length ? [cellOf(r.cells[i]!).value] : [])).filter((v) => v !== "");
    return i > 0 && values.length > 0 && values.every(figure);
  });
}

/** A table rebuilt from the paper. A cell's note shows under it on hover, tap or focus; the column under the pointer
 * is highlighted, so a number reads against its header; with `heat`, each number after the first column is coloured
 * by its size. */
export function Table({ block }: { block: BlockOf<"table"> }) {
  const wrap = useRef<HTMLDivElement>(null);
  const [col, setCol] = useState<number | null>(null);
  const [tip, setTip] = useState<{ text: string; left: number; top: number } | null>(null);

  const show = (td: HTMLElement) => {
    const box = wrap.current?.getBoundingClientRect();
    if (!box || !td.dataset.note) return;
    const r = td.getBoundingClientRect();
    // under the cell, kept inside the table's own width
    setTip({ text: td.dataset.note, left: Math.max(0, Math.min(r.left - box.left, box.width - 300)), top: r.bottom - box.top + 6 });
  };
  const noted = (target: EventTarget) => (target as Element).closest<HTMLElement>("td[data-note]");
  // a tap anywhere else puts the note away
  useEffect(() => {
    if (!tip) return;
    const away = (e: MouseEvent) => !wrap.current?.contains(e.target as Node) && setTip(null);
    document.addEventListener("click", away);
    return () => document.removeEventListener("click", away);
  }, [tip]);

  const heat = block.heat;
  const figures = figureColumns(block);
  const cls = (...names: (string | false | undefined)[]) => names.filter(Boolean).join(" ") || undefined;
  return (
    <>
      <div
        className="dr-tw"
        ref={wrap}
        onClick={(e) => {
          const td = noted(e.target);
          if (td) show(td);
          else setTip(null);
        }}
      >
        <div className="sc">
          <table
            className="dr-t"
            style={heat ? ({ "--hc": COLOUR[heat.colour ?? "red"] } as CSSProperties) : undefined}
            onMouseOver={(e) => {
              const cell = (e.target as Element).closest<HTMLTableCellElement>("td, th");
              // a group heading spans the table and has no column of its own
              setCol(cell && cell.cellIndex > 0 && cell.colSpan === 1 ? cell.cellIndex : null);
              const td = noted(e.target);
              if (td) show(td);
              else setTip(null);
            }}
            onMouseLeave={() => {
              setCol(null);
              setTip(null);
            }}
            onFocus={(e) => {
              const td = noted(e.target);
              if (td) show(td);
            }}
            onBlur={() => setTip(null)}
          >
            <thead>
              <tr>
                {block.columns.map((c, i) => (
                  <th key={i} className={cls(i === col && "hl", figures[i] && "num")}>
                    {c}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {block.rows.map((row, r) =>
                "group" in row ? (
                  <tr key={r} className="tg">
                    <td colSpan={block.columns.length}>{row.group}</td>
                  </tr>
                ) : (
                  <tr key={r} className={row.sum ? "sum" : undefined}>
                    {row.cells.map((c, i) => {
                      const { value, note } = cellOf(c);
                      const hot = heat && i > 0 && typeof value === "number" && value > 0;
                      return (
                        <td
                          key={i}
                          className={cls(i === col && "hl", hot && "heat", note && "tip", figures[i] && "num")}
                          data-note={note}
                          tabIndex={note ? 0 : undefined}
                          style={hot ? ({ "--heat": `${Math.round(Math.min(1, value / heat.max) * HEAT)}%` } as CSSProperties) : undefined}
                        >
                          {value === "" ? <span className="faint">·</span> : value}
                        </td>
                      );
                    })}
                  </tr>
                ),
              )}
            </tbody>
          </table>
        </div>
        {tip && (
          <div className="dr-ftip" role="tooltip" style={{ left: tip.left, top: tip.top }}>
            {tip.text}
          </div>
        )}
      </div>
      {block.caption && (
        <div className="dr-cap">
          <Inline text={block.caption} />
        </div>
      )}
    </>
  );
}
