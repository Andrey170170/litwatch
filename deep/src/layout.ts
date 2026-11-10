/**
 * Diagram layout, done once, here (docs/plans/deep-read.md, "Figures and equations"): a draft's diagram becomes
 * the stored read's, which always carries coordinates, so the web app only draws. A diagram whose nodes all carry
 * their place is kept exactly; any other is laid out with ELK's layered algorithm.
 */

import Elk, { type ElkNode } from "elkjs/lib/elk.bundled.js";
import { MIN_NODE_HEIGHT, placed } from "./checks.ts";
import type { Diagram, Draft, DraftDiagram, Read } from "./read.ts";

/** The room between two layers of boxes: an arrow keeps at least 40 px of length, so it can be tapped on a phone. */
const LAYER_GAP = 56;
const NODE_GAP = 24;
const MARGIN = 12;
/** The page draws a diagram across the read's column (710 px on the desktop), scaled to fit: a drawing much wider
 * than that shrinks its text, and a narrower one would be blown up, so it is centred in a drawing this wide. */
const MAX_WIDTH = 860;
const MIN_WIDTH = 700;
/** A drawing that wraps its layers into rows may run this wide before top-to-bottom is tried. */
const MAX_WRAPPED_WIDTH = 1000;

type Node = Diagram["nodes"][number];
type Edge = Diagram["edges"][number];

// Text widths are estimated from the page's font sizes (deepread.css: a node's label 12.5 px semibold, its second
// line 10.5 px, an edge's label 10 px); a render shows the agent the result.
const labelWidth = (label: string, sub?: string) => Math.max(label.length * 7.4, (sub?.length ?? 0) * 5.8);
const size = (n: DraftDiagram["nodes"][number]) => ({
  width: n.w ?? Math.min(Math.max(Math.ceil(labelWidth(n.label, n.sub)) + 24, 90), 240),
  height: n.h ?? (n.sub ? 52 : Math.max(44, MIN_NODE_HEIGHT)),
});

type Flow = "across" | "wrapped" | "down";

async function arrange(d: DraftDiagram, flow: Flow): Promise<Diagram> {
  // the bundle is CommonJS: its constructor is the module's `default`
  const elk = new Elk.default();
  const input: ElkNode = {
    id: "root",
    layoutOptions: {
      "elk.algorithm": "layered",
      "elk.direction": flow === "down" ? "DOWN" : "RIGHT",
      // a long chain is cut into rows, towards the column's shape
      ...(flow === "wrapped" ? { "elk.layered.wrapping.strategy": "MULTI_EDGE", "elk.aspectRatio": "2.6" } : {}),
      "elk.edgeRouting": "ORTHOGONAL",
      "elk.layered.spacing.nodeNodeBetweenLayers": String(LAYER_GAP),
      "elk.layered.spacing.edgeNodeBetweenLayers": "20",
      "elk.spacing.nodeNode": String(NODE_GAP),
      "elk.spacing.edgeLabel": "4",
      "elk.padding": `[top=${MARGIN},left=${MARGIN},bottom=${MARGIN},right=${MARGIN}]`,
    },
    children: d.nodes.map((n) => ({ id: n.id, ...size(n) })),
    edges: d.edges.map((e, i) => ({
      id: `e${i}`,
      sources: [e.from],
      targets: [e.to],
      ...(e.label ? { labels: [{ text: e.label, width: Math.ceil(e.label.length * 5.6) + 6, height: 14 }] } : {}),
    })),
  };
  const graph = await elk.layout(input);
  const at = new Map((graph.children ?? []).map((c) => [c.id, c]));
  const routes = new Map((graph.edges ?? []).map((e) => [e.id, e]));
  const r = Math.round;
  const width = Math.max(Math.ceil(graph.width ?? 0), MIN_WIDTH);
  const shift = (width - (graph.width ?? 0)) / 2;
  const nodes: Node[] = d.nodes.map((n) => {
    const box = at.get(n.id)!;
    return { ...n, x: r((box.x ?? 0) + shift), y: r(box.y ?? 0), w: r(box.width ?? 0), h: r(box.height ?? 0) };
  });
  const edges: Edge[] = d.edges.map((e, i) => {
    const route = routes.get(`e${i}`);
    const section = route?.sections?.[0];
    const label = route?.labels?.[0];
    return {
      ...e,
      ...(section ? { points: [section.startPoint, ...(section.bendPoints ?? []), section.endPoint].map((p) => [r(p.x + shift), r(p.y)] as [number, number]) } : {}),
      // the label's box, as the page draws a label: its baseline at the middle of the box's width
      ...(label ? { label_at: { x: r((label.x ?? 0) + shift + (label.width ?? 0) / 2), y: r((label.y ?? 0) + (label.height ?? 0) - 3), anchor: "middle" as const } } : {}),
    };
  });
  return { ...d, nodes, edges, width, height: Math.ceil(graph.height ?? 0) };
}

/** A draft's diagram with its coordinates: as given when every node is placed (the drawing's size taken from the
 * boxes when left out), else laid out left to right, in rows when that runs too wide, or top to bottom when the rows
 * do too. */
export async function layoutDiagram(d: DraftDiagram): Promise<Diagram> {
  if (placed(d)) {
    const nodes = d.nodes as Node[];
    return {
      ...d,
      nodes,
      width: d.width ?? Math.ceil(Math.max(...nodes.map((n) => n.x + n.w)) + MARGIN),
      height: d.height ?? Math.ceil(Math.max(...nodes.map((n) => n.y + n.h)) + MARGIN),
    };
  }
  const across = await arrange(d, "across");
  if (across.width <= MAX_WIDTH) return across;
  const wrapped = await arrange(d, "wrapped");
  if (wrapped.width <= MAX_WRAPPED_WIDTH) return wrapped;
  const down = await arrange(d, "down");
  return down.width < wrapped.width ? down : wrapped;
}

/** A draft that passed the checks as the read to store: every diagram with its coordinates. */
export async function layout(draft: Draft): Promise<Read> {
  const sections = await Promise.all(
    draft.sections.map(async (s) =>
      "blocks" in s ? { ...s, blocks: await Promise.all(s.blocks.map(async (b) => (b.type === "diagram" ? layoutDiagram(b) : b))) } : s,
    ),
  );
  return { ...draft, sections } as Read;
}
