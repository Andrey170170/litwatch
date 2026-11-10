import assert from "node:assert/strict";
import { test } from "node:test";
import { layout, layoutDiagram } from "../src/layout.ts";
import { readErrors, type Diagram, type DraftDiagram } from "../src/read.ts";
import { blocks, changed } from "./support.ts";

const node = (id: string, label = id) => ({ id, label, tip: `What ${label} does in the pipeline.` });
const edge = (from: string, to: string, label?: string) => ({ from, to, tip: `What passes from ${from} to ${to}.`, ...(label ? { label } : {}) });
const chain = (n: number): DraftDiagram => ({
  type: "diagram",
  nodes: Array.from({ length: n }, (_, i) => node(`n${i}`, `Stage ${i} of the method`)),
  edges: Array.from({ length: n - 1 }, (_, i) => edge(`n${i}`, `n${i + 1}`)),
});

/** The length of an edge's drawn route. */
const length = (points: [number, number][]) => points.slice(1).reduce((sum, p, i) => sum + Math.hypot(p[0] - points[i]![0], p[1] - points[i]![1]), 0);
/** Whether two boxes overlap. */
const overlap = (a: Diagram["nodes"][number], b: Diagram["nodes"][number]) => a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h;

test("a diagram without coordinates is laid out: every box placed and tall enough to tap, every arrow 40 px or longer", async () => {
  const d: DraftDiagram = {
    type: "diagram",
    nodes: [node("trace", "Trace"), node("stream", "Residual stream"), { ...node("probe", "Linear probe"), sub: "layer 12" }, node("random", "Random direction"), node("verdict", "Faithful or not")],
    edges: [edge("trace", "stream"), edge("stream", "probe", "h at each token"), edge("stream", "random"), edge("probe", "verdict"), edge("random", "verdict")],
  };
  const laid = await layoutDiagram(d);
  assert.deepEqual(laid.nodes.map((n) => n.id), d.nodes.map((n) => n.id), "the nodes as given, in their order");
  for (const n of laid.nodes) {
    assert.ok([n.x, n.y, n.w, n.h].every(Number.isFinite), `${n.id} is placed`);
    assert.ok(n.h >= 40, `${n.id} is ${n.h} px tall`);
    assert.ok(n.x >= 0 && n.y >= 0 && n.x + n.w <= laid.width && n.y + n.h <= laid.height, `${n.id} is inside the drawing`);
  }
  for (const [i, a] of laid.nodes.entries()) for (const b of laid.nodes.slice(i + 1)) assert.ok(!overlap(a, b), `${a.id} and ${b.id} do not overlap`);
  for (const e of laid.edges) {
    assert.ok(e.points && e.points.length >= 2, `${e.from}→${e.to} has its route`);
    assert.ok(length(e.points) >= 40, `${e.from}→${e.to} is ${length(e.points)} px long`);
  }
  assert.ok(laid.edges[1]!.label_at, "an edge's label is placed");
});

test("a long chain is wrapped to the column rather than drawn as a strip", async () => {
  const laid = await layoutDiagram(chain(10));
  assert.ok(laid.width <= 1000, `width ${laid.width}`);
  assert.ok(laid.height < laid.width, `a drawing ${laid.width} by ${laid.height} is not a tall strip`);
  for (const e of laid.edges) assert.ok(length(e.points!) >= 40, `${e.from}→${e.to} is ${length(e.points!)} px long`);
  // a small diagram is not blown up to the column's width: it is centred in a drawing as wide as the column
  const small = await layoutDiagram(chain(2));
  assert.equal(small.width, 700);
  const [left, right] = [small.nodes[0]!.x, small.width - (small.nodes[1]!.x + small.nodes[1]!.w)];
  assert.ok(Math.abs(left - right) <= 2, `margins ${left} and ${right}`);
});

test("a diagram whose nodes are all placed is kept as drawn", async () => {
  const d: DraftDiagram = {
    type: "diagram",
    nodes: [{ ...node("a"), x: 20, y: 30, w: 120, h: 44 }, { ...node("b"), x: 300, y: 30, w: 140, h: 60 }],
    edges: [{ ...edge("a", "b"), points: [[140, 52], [300, 60]] }],
  };
  const laid = await layoutDiagram(d);
  assert.deepEqual(laid.nodes, d.nodes);
  assert.deepEqual(laid.edges, d.edges);
  assert.deepEqual([laid.width, laid.height], [452, 102], "the drawing's size is the boxes' with a margin");
  assert.deepEqual([(await layoutDiagram({ ...d, width: 600, height: 200 })).width, (await layoutDiagram({ ...d, width: 600, height: 200 })).height], [600, 200]);
});

test("a draft laid out is a read the stored schema takes", async () => {
  const d = changed((r) => blocks(r, "method").push(chain(3) as unknown as Record<string, unknown>));
  assert.notDeepEqual(readErrors(d), [], "a stored diagram needs its coordinates");
  const read = await layout(d);
  assert.deepEqual(readErrors(read), []);
  assert.deepEqual(read.sections.map((s) => s.id), d.sections.map((s) => s.id));
});
