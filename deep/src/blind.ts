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
import { BlockSchema, SectionSchema, type Read } from "./read.ts";
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
      { maxItems: 20, description: "Last, and most important first; only the places that cost the thread." },
    ),
  },
  { additionalProperties: false },
);

export type Follow = Static<typeof FollowSchema>;

const TOOL = { name: "follow", description: "Give what you understood: the retelling, the followability score, then the places you lost the thread.", parameters: FollowSchema };

// what a reader does not see: layout, links, and a section's takeaway, which only the read's contents outside the web
// app show; a block's type is shown as its label
const UNREAD = new Set(["type", "kind", "x", "y", "w", "h", "width", "height", "label_at", "style", "colour", "anchor", "url", "code_url", "takeaway"]);

/** Each block's and section's fields in the schema's order, which is the order the web app shows them in, whatever
 * order the writer's JSON has them in. */
const ORDER = new Map<string, string[]>([
  ...(BlockSchema.anyOf as { properties: Record<string, { const?: string }> }[]).map((s) => [`block:${s.properties.type!.const}`, Object.keys(s.properties)] as const),
  ...(SectionSchema.anyOf as { properties: Record<string, { const?: string }> }[]).map((s) => [`section:${s.properties.kind!.const}`, Object.keys(s.properties)] as const),
]);
const ordered = (o: Record<string, unknown>, order: string[] = []): [string, unknown][] => {
  const at = (k: string) => (order.includes(k) ? order.indexOf(k) : order.length);
  return Object.entries(o).sort(([a], [b]) => at(a) - at(b));
};

/** The read as a reader meets it: its sections in order, each under its title and id, each block under its type
 * and place ("method.3"), a WHY box as its question with its folded answer, a figure as its caption beside its
 * image, the collection's papers under Related work. `figures` names the figure blocks' images in the order the
 * read shows them. */
export function readerText(read: Read): { text: string; figures: string[] } {
  const lines: string[] = [];
  const figures: string[] = [];
  const walk = (value: unknown, indent: string, key?: string): void => {
    if (key !== undefined && UNREAD.has(key)) return;
    const label = key === undefined ? "" : `${key}: `;
    if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") lines.push(`${indent}${label}${value}`);
    else if (Array.isArray(value)) {
      if (label) lines.push(`${indent}${key}:`);
      for (const v of value) walk(v, label ? `${indent}  ` : indent);
    } else if (value && typeof value === "object") {
      if (label) lines.push(`${indent}${key}:`);
      for (const [k, v] of Object.entries(value)) walk(v, label ? `${indent}  ` : indent, k);
    }
  };
  for (const section of read.sections) {
    lines.push("", `== ${section.title} == (${section.id})`);
    for (const [key, value] of ordered(section, ORDER.get(`section:${section.kind}`))) {
      if (key === "id" || key === "title") continue;
      if (key !== "blocks" || !Array.isArray(value)) {
        walk(value, "", key);
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
          for (const [k, v] of ordered(block, ORDER.get(`block:${String(block.type)}`))) walk(v, "  ", k);
        }
      });
    }
    if (section.kind === "related" && read.among_collection.length) walk(read.among_collection, "", "among the collection");
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
