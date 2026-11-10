import { useId, useMemo, useState, type CSSProperties, type KeyboardEvent } from "react";
import { Inline } from "./inline";
import { COLOUR, type BlockOf } from "./types";

type D = BlockOf<"diagram">;
type Node = D["nodes"][number];
type Edge = D["edges"][number];
type P = [number, number];

const centre = (n: Node): P => [n.x + n.w / 2, n.y + n.h / 2];
/** Where the straight line from a box's centre towards a point leaves the box. */
function leave(n: Node, to: P): P {
  const [cx, cy] = centre(n);
  const dx = to[0] - cx;
  const dy = to[1] - cy;
  if (!dx && !dy) return [cx, cy];
  const k = Math.min(dx ? n.w / 2 / Math.abs(dx) : Infinity, dy ? n.h / 2 / Math.abs(dy) : Infinity);
  return [cx + dx * k, cy + dy * k];
}
/** The point half way along a path, where a label with no place of its own is drawn. */
function middle(pts: P[]): P {
  const len = (a: P, b: P) => Math.hypot(b[0] - a[0], b[1] - a[1]);
  let left = pts.slice(1).reduce((sum, p, i) => sum + len(pts[i]!, p), 0) / 2;
  for (let i = 1; i < pts.length; i++) {
    const a = pts[i - 1]!;
    const b = pts[i]!;
    const d = len(a, b);
    if (left <= d) return d ? [a[0] + ((b[0] - a[0]) * left) / d, a[1] + ((b[1] - a[1]) * left) / d] : a;
    left -= d;
  }
  return pts[pts.length - 1]!;
}

/** A diagram the read draws, from the coordinates stored with it (the deep service lays it out; nothing is laid
 * out here). Clicking a node or an edge lights it and its neighbours, dims the rest and shows its explanation
 * under the drawing; clicking it again, or the background, puts it back. */
export function Diagram({ block }: { block: D }) {
  // useId's marks are not safe inside url(#…)
  const uid = useId().replace(/[^a-zA-Z0-9_-]/g, "");
  const [sel, setSel] = useState<string | null>(null);
  const nodes = useMemo(() => new Map(block.nodes.map((n) => [n.id, n])), [block.nodes]);
  // an edge between nodes the diagram does not have cannot be drawn
  const edges = useMemo(
    () =>
      block.edges.flatMap((e, i) => {
        const a = nodes.get(e.from);
        const b = nodes.get(e.to);
        if (!a || !b) return [];
        const pts = (e.points as P[] | undefined) ?? [leave(a, centre(b)), leave(b, centre(a))];
        return [{ e, key: `e:${i}`, pts, title: e.title ?? e.label ?? `${a.label} → ${b.label}` }];
      }),
    [block.edges, nodes],
  );
  const related = useMemo(() => {
    if (!sel) return null;
    const lit = new Set([sel]);
    for (const { e, key } of edges)
      if (key === sel || sel === `n:${e.from}` || sel === `n:${e.to}`) [key, `n:${e.from}`, `n:${e.to}`].forEach((k) => lit.add(k));
    return lit;
  }, [sel, edges]);
  const state = (key: string) => (sel === key ? " on" : related && !related.has(key) ? " dim" : "");
  const pick = (key: string) => ({
    role: "button",
    tabIndex: 0,
    "aria-pressed": sel === key,
    onClick: (ev: { stopPropagation: () => void }) => {
      ev.stopPropagation();
      setSel(sel === key ? null : key);
    },
    onKeyDown: (ev: KeyboardEvent) => {
      if (ev.key !== "Enter" && ev.key !== " ") return;
      ev.preventDefault();
      setSel(sel === key ? null : key);
    },
  });
  const node = sel?.startsWith("n:") ? nodes.get(sel.slice(2)) : undefined;
  const edge = edges.find((x) => x.key === sel);
  const shown: { title: string; tip: string; where?: string } | undefined = node ? { title: node.label, tip: node.tip, where: node.where } : edge ? { title: edge.title, tip: edge.e.tip, where: edge.e.where } : undefined;
  return (
    <>
      <div className="dr-dg">
        <div className="svw">
          <svg viewBox={`0 0 ${block.width} ${block.height}`} style={{ "--dw": `${block.width}px` } as CSSProperties} onClick={() => setSel(null)}>
            <defs>
              {["ah", "on"].map((m) => (
                <marker key={m} id={`${uid}-${m}`} className={`dr-${m}`} viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
                  <path d="M0,0 L10,5 L0,10 z" />
                </marker>
              ))}
            </defs>
            {block.lanes?.map((l, i) => (
              <text key={i} x={l.x} y={l.y} className="lane">
                {l.text}
              </text>
            ))}
            {edges.map(({ e, key, pts, title }) => {
              const d = pts.map((p, i) => `${i ? "L" : "M"}${p[0]},${p[1]}`).join(" ");
              const at = e.label_at ?? { x: middle(pts)[0], y: middle(pts)[1] - 6 };
              return (
                <g key={key} className={`eg ${e.style ?? "solid"}${state(key)}`} aria-label={title} {...pick(key)}>
                  <path d={d} className="ht" />
                  <path d={d} markerEnd={`url(#${uid}-${sel === key ? "on" : "ah"})`} />
                  {e.label && (
                    <text x={at.x} y={at.y} textAnchor={e.label_at?.anchor ?? "middle"}>
                      {e.label}
                    </text>
                  )}
                </g>
              );
            })}
            {block.nodes.map((n) => {
              const key = `n:${n.id}`;
              const cx = n.x + n.w / 2;
              return (
                <g key={key} className={`nd${state(key)}`} style={{ "--nc": COLOUR[n.colour ?? "neutral"] } as CSSProperties} aria-label={n.label} {...pick(key)}>
                  <rect x={n.x} y={n.y} width={n.w} height={n.h} rx={2} />
                  <text x={cx} y={n.y + (n.sub ? n.h / 2 - 2 : n.h / 2 + 4)} textAnchor="middle">
                    {n.label}
                  </text>
                  {n.sub && (
                    <text x={cx} y={n.y + n.h / 2 + 12} textAnchor="middle" className="s">
                      {n.sub}
                    </text>
                  )}
                </g>
              );
            })}
          </svg>
        </div>
        <div className="tipbox" aria-live="polite">
          {shown ? (
            <>
              <span className="tt">{shown.title}</span>
              <Inline text={shown.tip} />
              {shown.where && <span className="where">{shown.where}</span>}
            </>
          ) : (
            <span className="hint">Click a box or an arrow for what it does.</span>
          )}
        </div>
      </div>
      {block.caption && (
        <div className="dr-cap">
          <Inline text={block.caption} />
        </div>
      )}
    </>
  );
}
