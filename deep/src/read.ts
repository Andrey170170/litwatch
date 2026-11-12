/**
 * The Deep read (docs/specs/deep-read.md): the document the deep service stores for a paper and the web app draws.
 * This schema is the one source of its shape: `npm run read-schema` exports it as JSON Schema to
 * `web/src/deepread/read.schema.json`, and the web app's types are generated from that file.
 *
 * A read is a sequence of sections. The anchors (At a glance, Claims, Limitations, Related work, Reproducibility)
 * have a fixed shape each; the sections between them (problem, method, findings) and the lenses are made of typed
 * blocks. The card's shared fields (`process_notes`, `among_collection`, `code_url`) stay top-level in the shapes
 * the Deep card has, so what reads them from a card reads them from a read.
 *
 * Text fields marked "inline text" may use a small markup and nothing else: `**bold**`, `*italic*`, `` `code` ``,
 * `$tex$` for inline maths, and `[words](https://…)` for a link. HTML is shown as written, never interpreted.
 *
 * The schema comes in two variants, built by one function so they cannot drift. `ReadSchema` is the stored read.
 * `DraftSchema` is what the agent may submit: only the process notes a model may write (no `review` kind, no
 * `service` source), and a diagram's coordinates optional, since the service lays a diagram out before it stores
 * the read (`layout.ts`).
 */

import { Type, type Static, type TNumber } from "typebox";
import { AMONG_COLLECTION, CODE_URL, enumOf, MODEL_NOTES, NOTE_KINDS, PRIOR_WORK, schemaErrors } from "./schema.ts";

/** An inline-text field; the read's own description states the markup once. */
const text = (description: string) => Type.String({ minLength: 1, description });
/** A field shown as written, with no markup. */
const plain = (description: string) => Type.String({ minLength: 1, description });
const where = (description = "Where in the paper this rests: a section, figure or table, as the paper numbers it (\"§5.3, Table 2\").") =>
  Type.String({ minLength: 1, description });
const caption = (description: string) => Type.Optional(text(description));

export const SECTION_KINDS = ["glance", "problem", "method", "finding", "claims", "lens", "limits", "related", "repro"] as const;
export const BODY_KINDS = ["problem", "method", "finding"] as const;
export const VERDICTS = ["holds", "partly", "not shown", "overclaimed"] as const;
/** The colours a diagram node or a table's heat may take: names for the app's own tokens, never CSS. */
export const COLOURS = ["neutral", "accent", "red", "orange", "amber", "green", "cyan", "blue", "purple"] as const;
export const EDGE_STYLES = ["solid", "dash", "dot"] as const;
/** A read's process notes may also be `review`: the service's note on a read that still failed its review. */
export const READ_NOTE_KINDS = [...NOTE_KINDS, "review"] as const;

/** One schema, two variants: the stored read, or (`draft`) what the agent may submit. Each varying part is typed as
 * the stored read's, so `Read` is the stored shape and `Draft` is derived from it below. */
function build(draft: boolean) {
  /** A coordinate: required in a stored read, optional in a draft, where `hint` says what leaving it out does. */
  const laidOut = (options: { description: string; exclusiveMinimum?: number }, hint?: string): TNumber =>
    (draft ? Type.Optional(Type.Number({ ...options, description: hint ? `${options.description} ${hint}` : options.description })) : Type.Number(options)) as TNumber;

  // ── blocks ──────────────────────────────────────────────────────────────────────────────────────────

  const Paragraph = Type.Object({ type: Type.Literal("p"), text: text("A paragraph.") }, { additionalProperties: false });
  const Subheading = Type.Object({ type: Type.Literal("h3"), text: plain("A subheading inside a section, plain text.") }, { additionalProperties: false });
  const List = Type.Object(
    { type: Type.Literal("list"), items: Type.Array(text("One item."), { minItems: 1 }) },
    { additionalProperties: false },
  );
  // a WHY box is folded, its question in view; a stored read from before P6 may have none, and shows open
  const question = plain('The question the box answers, in view while it is folded: "Why mean ablation, not zero?"');
  const Why = Type.Object(
    {
      type: Type.Literal("why"),
      question: draft ? question : Type.Optional(question),
      text: text("The paper's reason for a choice a careful reader would question and the text around it leaves out."),
    },
    { additionalProperties: false },
  );
  const CardNote = Type.Object(
    {
      type: Type.Literal("note"),
      text: text("The read's own sceptical remark, where the paper's text does not say it."),
      where: where(),
    },
    { additionalProperties: false },
  );
  const Equation = Type.Object(
    {
      type: Type.Literal("eq"),
      tex: plain("The equation in TeX, as KaTeX reads it in display mode, without delimiters."),
      caption: caption("What the equation says, and what is empirical about it."),
    },
    { additionalProperties: false },
  );
  const Figure = Type.Object(
    {
      type: Type.Literal("fig"),
      name: Type.String({
        pattern: "^[A-Za-z0-9][A-Za-z0-9._-]*$",
        description: "The figure's name among the read's figures; its image is stored with the read under this name.",
      }),
      number: plain("The figure's number in the paper, as the paper labels it (\"Figure 6\")."),
      caption: text("A caption in the read's own words."),
    },
    { additionalProperties: false },
  );
  const Experiment = Type.Object(
    {
      type: Type.Literal("xp"),
      label: Type.Optional(plain("A short tag for the experiment (\"1a\").")),
      question: text("The question the experiment answers."),
      did: text("What was done."),
      found: text("The result, with its numbers."),
      means: text("What the result shows."),
      not_shown: Type.Optional(text("What it leaves open, including the controls that hold.")),
    },
    { additionalProperties: false },
  );

  const Cell = Type.Union(
    [
      Type.String(),
      Type.Number(),
      Type.Object(
        {
          value: Type.Union([Type.String(), Type.Number()]),
          note: plain("A short note on this cell, shown on hover or tap; plain text."),
        },
        { additionalProperties: false },
      ),
    ],
    { description: "A cell: its value (plain text or a number; an empty string for an empty cell), or its value with a note." },
  );
  const Row = Type.Union(
    [
      Type.Object({ group: plain("A heading across the table, over the rows that follow it.") }, { additionalProperties: false }),
      Type.Object(
        {
          cells: Type.Array(Cell, { minItems: 1, description: "One cell per column." }),
          sum: Type.Optional(Type.Boolean({ description: "True for a totals row, set in bold." })),
        },
        { additionalProperties: false },
      ),
    ],
    { description: "A row of cells, or a group heading." },
  );
  const Table = Type.Object(
    {
      type: Type.Literal("table"),
      columns: Type.Array(plain("A column's header, plain text."), { minItems: 2 }),
      rows: Type.Array(Row, { minItems: 1 }),
      heat: Type.Optional(
        Type.Object(
          {
            max: Type.Number({ exclusiveMinimum: 0, description: "The value coloured at full strength." }),
            colour: Type.Optional(enumOf(COLOURS, "The heat's colour; red when left out.")),
          },
          { additionalProperties: false, description: "Colours each numeric cell after the first column by its value, where the values compare." },
        ),
      ),
      caption: caption("Where the table comes from and how it was cut (\"Rebuilt from Table 1, eleven of its 22 rows\")."),
    },
    { additionalProperties: false },
  );

  const Point = Type.Tuple([Type.Number(), Type.Number()], { description: "A point, [x, y], in the diagram's units." });
  const Node = Type.Object(
    {
      id: Type.String({ pattern: "^[a-z][a-z0-9-]*$", description: "The node's name within its diagram, which edges refer to." }),
      label: plain("The node's name, plain text, one line."),
      sub: Type.Optional(plain("A second, smaller line under the name.")),
      colour: Type.Optional(enumOf(COLOURS, "The node's colour; neutral when left out.")),
      x: laidOut({ description: "The left edge of the node's box." }, "Leave x and y out on every node to have the diagram laid out for you."),
      y: laidOut({ description: "The top edge of the node's box." }),
      w: laidOut({ exclusiveMinimum: 0, description: "The box's width." }, "Left out, it follows the label."),
      h: laidOut({ exclusiveMinimum: 0, description: "The box's height, at least 40." }),
      tip: text("What the node does and the numbers behind it, shown when it is clicked."),
      where: Type.Optional(where("Where the paper shows it.")),
    },
    { additionalProperties: false },
  );
  const Edge = Type.Object(
    {
      from: plain("The id of the node the edge leaves."),
      to: plain("The id of the node the edge points at."),
      label: Type.Optional(plain("A few words drawn beside the edge, plain text.")),
      title: Type.Optional(plain("The heading of the edge's explanation; its label when left out, else the two nodes' names.")),
      style: Type.Optional(enumOf(EDGE_STYLES, "How the line is drawn; solid when left out.")),
      points: Type.Optional(
        Type.Array(Point, {
          minItems: 2,
          description: "The edge's path from start to end, through its waypoints. Left out, it is a straight line between the two boxes.",
        }),
      ),
      label_at: Type.Optional(
        Type.Object(
          {
            x: Type.Number(),
            y: Type.Number(),
            anchor: Type.Optional(enumOf(["start", "middle", "end"] as const, "Which part of the label sits at the point; middle when left out.")),
          },
          { additionalProperties: false, description: "Where the label is drawn; the middle of the path when left out." },
        ),
      ),
      tip: text("What passes along the edge and the numbers behind it, shown when it is clicked."),
      where: Type.Optional(where("Where the paper shows it.")),
    },
    { additionalProperties: false },
  );
  const Diagram = Type.Object(
    {
      type: Type.Literal("diagram"),
      width: laidOut({ exclusiveMinimum: 0, description: "The drawing's width in its own units; coordinates run from 0 to it." }, "Left out, it is the laid-out drawing's."),
      height: laidOut({ exclusiveMinimum: 0, description: "The drawing's height in its own units." }),
      nodes: Type.Array(Node, { minItems: 1 }),
      edges: Type.Array(Edge),
      lanes: Type.Optional(
        Type.Array(
          Type.Object(
            { text: plain("The lane's name."), x: Type.Number(), y: Type.Number() },
            { additionalProperties: false },
          ),
          { description: "Names of the diagram's bands, each drawn at its point." },
        ),
      ),
      caption: caption("What the diagram was drawn from."),
    },
    {
      additionalProperties: false,
      description: draft
        ? "A diagram the read draws: nodes and edges, each explained. Give no coordinates and it is laid out for you " +
          "(lanes, edge points and label_at are then left out too); give every node its x, y, w and h and they are " +
          "used exactly."
        : "A diagram the read draws: nodes and edges, each explained. A stored read always carries coordinates: the " +
          "deep service lays a diagram out before it stores the read, and the web app only draws.",
    },
  );

  const BlockSchema = Type.Union([Paragraph, Subheading, List, Why, CardNote, Equation, Figure, Experiment, Table, Diagram]);
  const Blocks = Type.Array(BlockSchema, { minItems: 1, description: "The section's content, in reading order." });

  // ── sections ────────────────────────────────────────────────────────────────────────────────────────

  const HEAD = {
    id: Type.String({
      pattern: "^[a-z][a-z0-9-]*$",
      description: "The section's name within the read, unique in it; the page links to it and a render targets it.",
    }),
    title: plain("The section's name as shown (\"Finding 2 · A softmax law for the read\")."),
  };
  const takeaway = text("The section in one line; the contents of the read outside the web app show it.");

  const Fact = Type.Object(
    {
      name: plain("What the fact is about (\"Models\", \"Code\")."),
      text: Type.Optional(text("The fact.")),
      tags: Type.Optional(
        Type.Array(
          Type.Object(
            {
              text: plain("A few words (\"code\", \"labelled traces\")."),
              state: Type.Optional(enumOf(["yes", "no"] as const, "Whether the thing named is there (yes) or missing (no); left out for a plain tag.")),
            },
            { additionalProperties: false },
          ),
          { minItems: 1, description: "Short marks in place of, or after, the text: what is released and what is not." },
        ),
      ),
    },
    { additionalProperties: false },
  );
  const Facts = Type.Array(Fact, { minItems: 1 });

  const Glance = Type.Object(
    {
      ...HEAD,
      kind: Type.Literal("glance"),
      claim: text("The paper's key claim in one sentence."),
      points: Type.Array(text("One point that carries the paper's argument."), { minItems: 2, maxItems: 6 }),
      facts: Facts,
    },
    { additionalProperties: false, description: "At a glance, always first: the key claim, the points and the facts (kind, models, task or data, methods, code, reading time)." },
  );
  const Body = Type.Object(
    { ...HEAD, kind: enumOf(BODY_KINDS, "What the section is: the problem and motivation, the method, or one finding."), takeaway, blocks: Blocks },
    { additionalProperties: false, description: "A section of the paper's own story, made of blocks." },
  );
  const Claims = Type.Object(
    {
      ...HEAD,
      kind: Type.Literal("claims"),
      takeaway,
      claims: Type.Array(
        Type.Object(
          {
            text: text("The claim, as the paper makes it."),
            verdict: enumOf(VERDICTS, "Whether the paper shows what it claims."),
            evidence: text("What the verdict rests on."),
            where: where(),
          },
          { additionalProperties: false },
        ),
        { minItems: 1, description: "One entry per main claim of the paper." },
      ),
    },
    { additionalProperties: false, description: "The claims ledger." },
  );
  const Lens = Type.Object(
    {
      ...HEAD,
      kind: Type.Literal("lens"),
      topic: plain("The slug of the topic the paper is graded in; the section takes that topic's colour."),
      takeaway,
      blocks: Blocks,
    },
    { additionalProperties: false, description: "What the paper means for one topic's question; its title is the topic's name." },
  );
  const Limits = Type.Object(
    {
      ...HEAD,
      kind: Type.Literal("limits"),
      takeaway,
      limitations: Type.Array(
        Type.Object(
          {
            by: enumOf(["paper", "read"] as const, "Whose it is: one the paper states, or one the read sees."),
            text: text("The limitation."),
          },
          { additionalProperties: false },
        ),
        { minItems: 1 },
      ),
    },
    { additionalProperties: false },
  );
  const Related = Type.Object(
    {
      ...HEAD,
      kind: Type.Literal("related"),
      takeaway,
      // the Deep card's shape, unchanged; the collection's papers are the read's top-level `among_collection`
      prior_work: PRIOR_WORK,
    },
    { additionalProperties: false, description: "The closest prior work. The page shows the read's among_collection under it." },
  );
  const Repro = Type.Object(
    { ...HEAD, kind: Type.Literal("repro"), takeaway, facts: Facts },
    { additionalProperties: false, description: "Reproducibility: what was checked, not assumed (code, data, models, compute, statistics)." },
  );

  const SectionSchema = Type.Union([Glance, Body, Claims, Lens, Limits, Related, Repro]);

  // ── the read ────────────────────────────────────────────────────────────────────────────────────────

  const ProcessNote = Type.Object(
    {
      kind: enumOf(READ_NOTE_KINDS, "What held the run back."),
      source: enumOf(["model", "service"] as const, "Who wrote the note: the agent, or the service after the run."),
      note: plain("One sentence: what could not be done or checked, and what the read says because of it."),
      url: Type.Optional(Type.String({ pattern: "^https?://\\S+$", description: "The page concerned, an absolute http(s) URL." })),
    },
    { additionalProperties: false },
  );

  const storedNotes = Type.Optional(
    Type.Array(ProcessNote, { description: "What the run could not do because of its tools, access or budget, not what the paper lacks." }),
  );
  const ReadSchema = Type.Object(
    {
      format: Type.Literal("read", { description: "Marks a Deep read; a Deep card from before has no format." }),
      sections: Type.Array(SectionSchema, {
        minItems: 1,
        description:
          "The read, in order: At a glance; the body (problem, method and finding sections, in the order a newcomer understands the argument); Claims; one lens per graded topic; Limitations; " +
          "Related work; Reproducibility.",
      }),
      among_collection: AMONG_COLLECTION,
      code_url: CODE_URL,
      process_notes: (draft ? MODEL_NOTES : storedNotes) as typeof storedNotes,
    },
    {
      additionalProperties: false,
      title: "Read",
      description:
        "A Deep read. Its prose fields are inline text: **bold**, *italic*, `code`, $tex$ for inline maths and " +
        "[words](https://…) for a link, and nothing else; HTML is shown as written. Names, labels, headers, table " +
        "cells, cell notes and paper locations are plain text.",
    },
  );
  return { ReadSchema, SectionSchema, BlockSchema };
}

/** The stored read. */
export const { ReadSchema, SectionSchema, BlockSchema } = build(false);
/** What the agent may submit: the read before the service lays its diagrams out and adds its own notes. */
export const DraftSchema = build(true).ReadSchema;

export type Read = Static<typeof ReadSchema>;
export type Section = Static<typeof SectionSchema>;
export type Block = Static<typeof BlockSchema>;
export type BlockOf<T extends Block["type"]> = Extract<Block, { type: T }>;
export type Diagram = BlockOf<"diagram">;

type Loose<T, K extends keyof T> = Omit<T, K> & Partial<Pick<T, K>>;
/** A diagram as a draft may give it: laid out by the service unless every node carries its coordinates. */
export type DraftDiagram = Loose<Omit<Diagram, "nodes">, "width" | "height"> & { nodes: Loose<Diagram["nodes"][number], "x" | "y" | "w" | "h">[] };
export type DraftBlock = Exclude<Block, Diagram> | DraftDiagram;
export type DraftSection = Section extends infer S ? (S extends { blocks: Block[] } ? Omit<S, "blocks"> & { blocks: DraftBlock[] } : S) : never;
/** A read as the agent submits it (`DraftSchema`). */
export type Draft = Omit<Read, "sections" | "process_notes"> & { sections: DraftSection[]; process_notes?: Static<typeof MODEL_NOTES> };

/** A draft's errors against `DraftSchema`, one line each, empty when it is one. */
export const draftErrors = (input: unknown): string[] => schemaErrors(DraftSchema, input);
/** A stored read's errors against `ReadSchema`. */
export const readErrors = (input: unknown): string[] => schemaErrors(ReadSchema, input);
