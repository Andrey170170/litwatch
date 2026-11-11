/**
 * The blind reader (docs/specs/deep-read.md, "A blind reader"): one call on the writer's model that reads the read
 * without the paper, as a reader meets it, and answers through a single `follow` tool with its retelling of the
 * paper's argument, the places it lost the thread, and a followability score. The reviewer reads with the paper
 * beside it and cannot see what a reader without the paper misses; this call can.
 */

import type { ImageContent, Model, Models, TextContent, ThinkingLevel } from "@earendil-works/pi-ai";
import { Type, type Static } from "typebox";
import type { Shot } from "./imaging.ts";
import { blindPrompt } from "./prompt.ts";
import { BlockSchema, ReadSchema, SectionSchema, type Read } from "./read.ts";
import { ask, type Asked } from "./review.ts";

const FollowSchema = Type.Object(
  {
    retelling: Type.String({ minLength: 1, description: "The paper's argument in a few sentences, as the read told it: the question, the idea, how it is tested, what it found." }),
    followability: Type.Integer({ minimum: 1, maximum: 5, description: "How well a reader without the paper follows the read's argument." }),
    findings: Type.Array(
      Type.Object(
        {
          where: Type.String({ minLength: 1, description: 'The place in the read: a section\'s id, or "<section id>.<n>" for its n-th block, counted from 0.' }),
          text: Type.String({ minLength: 1, description: "Where the thread was lost and why: what was missing or came too early." }),
        },
        { additionalProperties: false },
      ),
      { description: "Last, and most important first; only the places that cost the thread." },
    ),
  },
  { additionalProperties: false },
);

export type Follow = Static<typeof FollowSchema>;

const TOOL = { name: "follow", description: "Give what you understood: the retelling, the followability score, then the places you lost the thread.", parameters: FollowSchema };

// what a reader does not see: layout, links, and a section's takeaway, which only the read's contents outside the web
// app show; a block's type is shown as its label
const UNREAD = new Set(["type", "kind", "x", "y", "w", "h", "width", "height", "label_at", "style", "colour", "anchor", "url", "code_url", "takeaway", "heat"]);
/** What the page folds, opening on a click. */
const FOLDED = new Set(["evidence"]);

type Node = { properties?: Record<string, Node>; anyOf?: Node[]; items?: Node; const?: unknown };

/** The schema of `value` among a union's branches: the one whose `type` or `kind` it has. */
function branch(schema: Node | undefined, value: Record<string, unknown>): Node | undefined {
  if (!schema?.anyOf) return schema;
  return schema.anyOf.find((s) => ["type", "kind"].some((k) => s.properties?.[k]?.const !== undefined && s.properties[k]!.const === value[k])) ?? schema.anyOf.find((s) => s.properties);
}

/** An object's fields in its schema's order, which is the order the web app shows them in, whatever order the
 * writer's JSON has them in; fields the schema does not name follow. */
function ordered(value: Record<string, unknown>, schema: Node | undefined): [string, unknown, Node | undefined][] {
  const props = schema?.properties ?? {};
  const order = Object.keys(props);
  const at = (k: string) => (order.includes(k) ? order.indexOf(k) : order.length);
  return Object.entries(value)
    .sort(([a], [b]) => at(a) - at(b))
    .map(([k, v]) => [k, v, props[k]]);
}

/** Coordinates (a diagram edge's waypoints), which say nothing to a reader. */
const coordinates = (v: unknown): boolean => Array.isArray(v) && v.length > 0 && v.every((x) => typeof x === "number" || coordinates(x));

/** The read as a reader meets it: its sections in order, each under its title and id, each block under its type
 * and place ("method.3"), every field in the order the page shows it, a WHY box as its question with its folded
 * answer, a figure as its caption beside its image, the collection's papers under Related work. `figures` names the
 * figure blocks' images in the order the read shows them. */
export function readerText(read: Read): { text: string; figures: string[] } {
  const lines: string[] = [];
  const figures: string[] = [];
  const walk = (value: unknown, schema: Node | undefined, indent: string, key?: string): void => {
    if ((key !== undefined && UNREAD.has(key)) || coordinates(value)) return;
    const name = key !== undefined && FOLDED.has(key) ? `${key}, shown on a click` : key;
    const label = name === undefined ? "" : `${name}: `;
    const inner = label ? `${indent}  ` : indent;
    if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") lines.push(`${indent}${label}${value}`);
    else if (Array.isArray(value)) {
      if (label) lines.push(`${indent}${name}:`);
      for (const v of value) {
        // each item of a list of objects (facts, nodes, claims) opens with a dash, so one ends where the next begins
        const start = lines.length;
        walk(v, schema?.items, inner);
        if (v && typeof v === "object" && !Array.isArray(v) && lines.length > start) lines[start] = lines[start]!.replace(/^(\s*)/, "$1- ");
      }
    } else if (value && typeof value === "object") {
      if (label) lines.push(`${indent}${name}:`);
      const o = value as Record<string, unknown>;
      for (const [k, v, s] of ordered(o, branch(schema, o))) walk(v, s, inner, k);
    }
  };
  const sectionSchema = SectionSchema as unknown as Node;
  const blockSchema = BlockSchema as unknown as Node;
  for (const section of read.sections) {
    lines.push("", `== ${section.title} == (${section.id})`);
    for (const [key, value, schema] of ordered(section, branch(sectionSchema, section))) {
      if (key === "id" || key === "title") continue;
      if (key !== "blocks" || !Array.isArray(value)) {
        walk(value, schema, "", key);
        continue;
      }
      value.forEach((block: Record<string, unknown>, n) => {
        const at = `${section.id}.${n}`;
        if (block.type === "why") {
          lines.push(`[WHY box, folded] (${at}) ${block.question ?? "Why?"}`, `  answer, shown on a click: ${block.text}`);
        } else if (block.type === "fig") {
          figures.push(String(block.name));
          lines.push(`[figure ${figures.length}, its image below] (${at}) ${block.number ?? ""}`.trimEnd(), `  caption: ${block.caption}`);
        } else {
          lines.push(`[${block.type}] (${at})`);
          for (const [k, v, s] of ordered(block, branch(blockSchema, block))) walk(v, s, "  ", k);
        }
      });
    }
    if (section.kind === "related" && read.among_collection.length) walk(read.among_collection, (ReadSchema as unknown as Node).properties?.among_collection, "", "among the collection");
  }
  return { text: lines.join("\n").trimStart(), figures };
}

export interface BlindInput {
  read: Read;
  /** The image of each figure the read shows, by its name. */
  shots: Map<string, Shot>;
}

/** Ask the blind reader what it understood of the read. */
export async function follow(
  models: Models,
  model: Model<string>,
  input: BlindInput,
  opts: { effort: ThinkingLevel; signal?: AbortSignal; onPayload?: (payload: unknown) => unknown },
): Promise<Asked<Follow>> {
  const { text, figures } = readerText(input.read);
  const content: (TextContent | ImageContent)[] = [{ type: "text", text: `The Deep read, as it is laid out:\n\n${text}` }];
  figures.forEach((name, i) => {
    const shot = input.shots.get(name);
    if (shot) content.push({ type: "text", text: `Figure ${i + 1} of the read:` }, { type: "image", data: shot.data, mimeType: shot.mimeType });
  });
  return ask<Follow>(models, model, { who: "the blind reader", systemPrompt: blindPrompt(), tool: TOOL, content }, opts);
}
