/**
 * The contracts the deep service shares with litwatch (docs/plans/a2-work-gate.md, D2): what a job is sent
 * (`POST /process`), the answer to a submission, the job as `GET /jobs/<id>` reports it, the names of a read that
 * litwatch reads its stored reads by, and a release-link find (`POST /links`, `GET /links/<id>`) with its answer. These schemas are the one source of each: the service checks a submission
 * against `JobInputSchema`, and `npm run contracts` exports every schema in `CONTRACTS` to
 * `contracts/deep.schema.json`, from which litwatch's models are generated (`scripts/contracts.sh` at the repository's
 * root regenerates everything). A new contract is one schema here, added to `CONTRACTS`, and one regenerate.
 *
 * The read itself is `read.ts`'s: litwatch keeps it as JSON and checks only its format tag, so a job's `result` is
 * declared here by that tag alone, and a read's section kinds are taken from its schema.
 *
 * Offsets into the paper's text are UTF-16 code units, as JavaScript indexes a string; litwatch converts its own.
 */

import { Type, type Static, type TSchema } from "typebox";
import { FIGURE_NAME, LIMITATION_BY, READ_NOTE_KINDS, ReadSchema, SectionSchema as ReadSectionSchema } from "./read.ts";
import { enumOf, RELATIONS } from "./schema.ts";

const nullable = <T extends TSchema>(schema: T) => Type.Optional(Type.Union([schema, Type.Null()]));
/** An offset into the paper's text. */
const offset = (description: string) => Type.Integer({ minimum: 0, description: `${description}, in UTF-16 code units.` });

// ── what a job is sent ──────────────────────────────────────────────────────────────────────────────

export const SectionSchema = Type.Object(
  {
    title: Type.String({ description: "The heading's title as the source writes it, comments dropped and spaces collapsed." }),
    depth: Type.Integer({ minimum: 0, description: "Its depth below the source's top level of headings, from 0." }),
    number: Type.Optional(Type.String({ description: 'Its number where LaTeX\'s defaults give it ("3.2", "A.1"); left out where the source numbers its sections itself.' })),
    start: offset("Where the heading starts in the paper's text"),
    end: offset("Where its section ends: at the next heading of its depth or above"),
  },
  { additionalProperties: false, description: "One heading of a LaTeX source: a sectioning command, the abstract or the bibliography." },
);

const OutlineSchema = Type.Object(
  {
    sections: Type.Array(SectionSchema, { description: "The headings in the order of the text." }),
    references: Type.Optional(
      Type.Object(
        { start: offset("Where it starts"), end: offset("Where it ends") },
        { additionalProperties: false, description: "The bibliography's environment, from its \\begin to its \\end." },
      ),
    ),
    appendix: Type.Optional(offset("Where the appendix starts (\\appendix, or a section named for it), which runs to the end")),
  },
  {
    additionalProperties: false,
    description: "A LaTeX source's outline, as litwatch parses it (src/litwatch/core/outline.py); the service reads a LaTeX paper by it.",
  },
);

const PAPER = {
  id: Type.String({ minLength: 1 }),
  title: Type.String({ description: "The paper's title, which arXiv may give empty." }),
  abstract: Type.String(),
  authors: Type.Array(Type.String()),
  published: nullable(Type.String()),
  url: nullable(Type.String()),
  body: Type.String({ description: "The paper's full text, as the archive gives it." }),
  body_version: nullable(Type.Integer({ minimum: 1, description: "The arXiv version the text is of, which the figures are taken at; null when litwatch cannot say." })),
};
const LatexPaperSchema = Type.Object(
  { ...PAPER, body_source: Type.Literal("latex"), outline: OutlineSchema },
  { additionalProperties: false, description: "A paper read from its LaTeX source, which comes with its outline." },
);
const PdfPaperSchema = Type.Object(
  { ...PAPER, body_source: Type.Literal("pdf") },
  { additionalProperties: false, description: "A paper read from its PDF's text, which has no outline." },
);
const PaperSchema = Type.Union([LatexPaperSchema, PdfPaperSchema], { description: "The paper, by where its text comes from." });

const SelectionSchema = Type.Object(
  {
    name: Type.String({ minLength: 1 }),
    slug: nullable(Type.String()),
    subject: Type.String(),
    rubric: nullable(Type.String()),
    grade: nullable(enumOf(["must_read", "relevant", "adjacent"] as const, "The grade litwatch gave the paper in the topic.")),
  },
  { additionalProperties: false, description: "A topic the read is for: why the lab follows it, what it must read, and the paper's grade there." },
);

export const JobInputSchema = Type.Object(
  {
    paper: PaperSchema,
    extensions: Type.Optional(Type.Array(Type.String(), { description: "The lens questions asked for, by name (prompt.ts, LENS_QUESTIONS)." })),
    selections: Type.Optional(Type.Array(SelectionSchema, { description: "The topics the read is written for." })),
  },
  { additionalProperties: false, description: "What POST /process is sent: one paper to read." },
);

// ── what a job reports ──────────────────────────────────────────────────────────────────────────────

const JOB_STATUSES = ["queued", "running", "done", "error"] as const;
const JobStatus = enumOf(JOB_STATUSES, "Where the job is.");

export const JobRefSchema = Type.Object(
  { id: Type.String(), status: JobStatus },
  { description: "A job as a submission's answer and GET /keys/<key> name it." },
);

const count = Type.Integer({ minimum: 0 });
const StatsSchema = Type.Object(
  { cells: count, turns: count, input: count, output: count, reasoning: count, cache_read: count, list_cost: Type.Number({ minimum: 0 }), seconds: count },
  { description: "A job's usage: REPL cells, model turns, tokens, the cost at list prices in dollars, and its seconds." },
);

const FigureSchema = Type.Object(
  {
    name: Type.String({ pattern: FIGURE_NAME, description: "The name the read's fig blocks cite it by." }),
    mime: Type.Literal("image/webp"),
    data: Type.String({ description: "The image, base64." }),
  },
  { description: "One of the read's figures." },
);

const FAILURES = ["service", "paper"] as const;

const ResultSchema = Type.Object(
  { format: ReadSchema.properties.format },
  { additionalProperties: true, description: "The Deep read (read.ts; its JSON Schema is web/src/deepread/read.schema.json), told by its format tag." },
);

export const JobSchema = Type.Object(
  {
    id: Type.String(),
    paper_id: Type.String(),
    title: Type.String(),
    extensions: Type.Array(Type.String()),
    status: JobStatus,
    created_at: Type.String(),
    started_at: Type.Optional(Type.String()),
    finished_at: Type.Optional(Type.String()),
    model: Type.Optional(Type.String()),
    stats: StatsSchema,
    last_event: Type.Optional(Type.String()),
    last_activity: Type.String(),
    result: nullable(ResultSchema),
    figures: Type.Optional(Type.Array(FigureSchema, { description: "The figures the read shows; absent when it shows none." })),
    reviews: nullable(Type.Array(Type.Unknown(), { description: "The review rounds in order (review.ts); null when no review gave a verdict." })),
    trace: Type.Optional(Type.Array(Type.Unknown(), { description: "What the job did, event by event (agent.ts, TraceEvent)." })),
    error: Type.Optional(Type.String()),
    failure: Type.Optional(enumOf(FAILURES, "Whose failure an error job's was: the service's, or the paper's.")),
    key: Type.Optional(Type.String()),
  },
  { description: "A job as GET /jobs/<id> reports it; a finished one carries what litwatch stores of it." },
);

// ── release links ───────────────────────────────────────────────────────────────────────────────────

export const LINK_KINDS = ["code", "weights"] as const;
export const LINK_STATES = ["public", "gated", "placeholder", "missing"] as const;
const LinkKind = enumOf(LINK_KINDS, "What the link releases: the paper's code, or its model weights.");
const LinkState = enumOf(
  LINK_STATES,
  "Whether it can be had: public, open to anyone now; gated, behind a request form or a licence to accept; " +
    "placeholder, a repository with nothing but a README; missing, a link the paper gives that leads nowhere.",
);
/** A link as the finder gives it: `ReleaseLinkSchema` but when it was checked, which the service adds. */
/** An absolute http(s) URL, and a time in UTC as RFC 3339 writes it (`2026-11-26T03:00:00Z`, fractions allowed). */
export const WEB_URL = "^[Hh][Tt][Tt][Pp][Ss]?://[^\\s/?#]+[^\\s]*$";
export const UTC_TIME = "^\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}:\\d{2}(\\.\\d{1,9})?Z$";

export const LINK_FIELDS = {
  url: Type.String({ pattern: WEB_URL, description: "The repository's or model's page, an absolute http(s) URL." }),
  kind: LinkKind,
  state: LinkState,
  license: nullable(Type.String({ minLength: 1, description: "Its license as the host states it (an SPDX id where it gives one); left out when it is not cheap to see." })),
};

export const ReleaseLinkSchema = Type.Object(
  { ...LINK_FIELDS, checked_at: Type.String({ pattern: UTC_TIME, description: "When the service last fetched it, an RFC 3339 time in UTC." }) },
  { additionalProperties: false, description: "A link to a paper's released code or weights, with its access state." },
);

export const LinksAnswerSchema = Type.Object(
  {
    links: Type.Array(ReleaseLinkSchema, { description: "Each link found and checked; empty when none was found." }),
    note: Type.Optional(Type.String({ description: "What the finder could not settle, in a sentence or two." })),
  },
  { additionalProperties: false, description: "What a find of a paper's release links answers." },
);

export const LinksInputSchema = Type.Object(
  {
    paper: PaperSchema,
    comments: Type.Optional(nullable(Type.String({ description: "The paper's comments on arXiv, where links are often given; none when absent." }))),
  },
  { additionalProperties: false, description: "What POST /links is sent: one paper to find the release links of." },
);

export const LinkJobSchema = Type.Object(
  {
    id: Type.String(),
    paper_id: Type.String(),
    status: JobStatus,
    created_at: Type.String(),
    started_at: Type.Optional(Type.String()),
    finished_at: Type.Optional(Type.String()),
    model: Type.Optional(Type.String()),
    stats: StatsSchema,
    last_event: Type.Optional(Type.String()),
    last_activity: Type.String(),
    result: nullable(LinksAnswerSchema),
    trace: Type.Optional(Type.Array(Type.Unknown(), { description: "What the find did, event by event (links.ts)." })),
    error: Type.Optional(Type.String()),
    failure: Type.Optional(enumOf(FAILURES, "Whose failure an error job's was: the service's, or the paper's.")),
    key: Type.Optional(Type.String()),
  },
  { description: "A find as GET /links/<id> reports it; a done one carries its answer." },
);

// ── a read's names ──────────────────────────────────────────────────────────────────────────────────

/** The strings a schema of string literals allows: a literal's, or each of a union's. */
const literals = (schema: TSchema): string[] =>
  "const" in schema ? [String(schema.const)] : "anyOf" in schema ? (schema.anyOf as TSchema[]).flatMap(literals) : [];
const SectionKind = enumOf(
  ReadSectionSchema.anyOf.flatMap((section) => literals(section.properties.kind)),
  "A read section's kind.",
);
const NoteKind = enumOf(READ_NOTE_KINDS, "A read's process note's kind.");
const Relation = enumOf(RELATIONS, "How a paper stands to one of the collection's.");
const LimitationBy = enumOf(LIMITATION_BY, "Whose a limitation is: the paper's own, or the read's.");
const ReadFormat = ReadSchema.properties.format;

/** Every contract, by the name its definition has in `contracts/deep.schema.json`. A schema nested in another that
 * is one of these is exported as a reference to it. */
export const CONTRACTS: Record<string, TSchema> = {
  JobInput: JobInputSchema,
  Paper: PaperSchema,
  LatexPaper: LatexPaperSchema,
  PdfPaper: PdfPaperSchema,
  Outline: OutlineSchema,
  Section: SectionSchema,
  Selection: SelectionSchema,
  JobRef: JobRefSchema,
  Job: JobSchema,
  Stats: StatsSchema,
  Figure: FigureSchema,
  Result: ResultSchema,
  SectionKind,
  NoteKind,
  Relation,
  LimitationBy,
  ReadFormat,
  LinksInput: LinksInputSchema,
  LinkJob: LinkJobSchema,
  LinksAnswer: LinksAnswerSchema,
  ReleaseLink: ReleaseLinkSchema,
  LinkKind,
  LinkState,
};

export type JobInput = Static<typeof JobInputSchema>;
export type LinksInput = Static<typeof LinksInputSchema>;
export type LinksAnswer = Static<typeof LinksAnswerSchema>;
export type ReleaseLink = Static<typeof ReleaseLinkSchema>;
export type PaperInput = Static<typeof PaperSchema>;
export type Outline = Static<typeof OutlineSchema>;
export type SelectionContext = Static<typeof SelectionSchema>;
export type Stats = Static<typeof StatsSchema>;
export type ReturnedFigure = Static<typeof FigureSchema>;
export type Failure = (typeof FAILURES)[number];
