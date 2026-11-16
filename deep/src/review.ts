/**
 * The reviewer (docs/specs/deep-read.md, "A reviewer"): one call on the writer's model, with no session, that reads
 * the read against the paper and the rubric and answers through a single `review` tool. Its answer is validated
 * against the tool's schema; the pass rule is the service's, not the model's: every score at least 4, the blind
 * reader's followability included (`blind.ts`), and every check passed.
 */

import type { AssistantMessage, ImageContent, Message, Model, Models, TextContent, ThinkingLevel } from "@earendil-works/pi-ai";
import { Type, type Static, type TSchema } from "typebox";
import type { Shot } from "./imaging.ts";
import { LENGTH_MAX, type Length } from "./length.ts";
import { reviewerMessage, reviewerPrompt, reviewerText, type PaperInput, type SelectionContext } from "./prompt.ts";
import type { Read } from "./read.ts";
import { enumOf, schemaErrors } from "./schema.ts";

/** The reviewer's scores; the blind reader adds followability. */
export const REVIEWER_SCORES = ["faithfulness", "clarity", "depth", "calibration"] as const;
export const SCORES = [...REVIEWER_SCORES, "followability"] as const;
/** The structure checks the reviewer makes, apart from what `submit` checks in code. */
export const REVIEW_CHECKS = ["anchors", "experiments", "why_boxes", "voice", "length"] as const;
/** A read passes with every score at this or above. */
export const PASS_SCORE = 4;

const score = (description: string) => Type.Integer({ minimum: 1, maximum: 5, description });
const VerdictSchema = Type.Object(
  {
    scores: Type.Object(
      {
        faithfulness: score("Numbers, claims and setups match the paper."),
        clarity: score("Free of the three prose faults; terms defined before use."),
        depth: score("Method and results close to the paper's detail."),
        calibration: score("Verdicts and CARD notes fair to the paper."),
      },
      { additionalProperties: false },
    ),
    checks: Type.Object(
      {
        anchors: Type.Boolean({ description: "The sections in their places, each holding what its name says." }),
        experiments: Type.Boolean({ description: "Findings given as experiments." }),
        why_boxes: Type.Boolean({ description: "WHY boxes and CARD notes only where they are meant, with no quota." }),
        voice: Type.Boolean({ description: "Objective: no reader addressed, no audience, no advice to read, no hype." }),
        length: Type.Boolean({ description: "In proportion to the paper." }),
      },
      { additionalProperties: false, description: "True: passed." },
    ),
    findings: Type.Array(
      Type.Object(
        {
          about: enumOf([...REVIEWER_SCORES, ...REVIEW_CHECKS] as const, "The score or check the finding counts against."),
          where: Type.String({ minLength: 1, description: 'The place in the read: a section\'s id, or "<section id>.<n>" for its n-th block, counted from 0.' }),
          text: Type.String({ minLength: 1, description: "What is wrong and what would fix it; a clarity finding names its fault and quotes the sentence." }),
          rewrite: Type.Optional(Type.String({ minLength: 1, description: "The sentence rewritten, for a clarity finding." })),
        },
        { additionalProperties: false },
      ),
      { maxItems: 30, description: "Last, and most important first; only what is worth acting on." },
    ),
  },
  { additionalProperties: false },
);

/** The reviewer's verdict. */
export type Verdict = Static<typeof VerdictSchema>;
type Finding = Verdict["findings"][number];

/** A round's verdict: the reviewer's, with the blind reader's score, findings and retelling joined to it. */
export interface Judgement extends Omit<Verdict, "scores" | "findings"> {
  scores: Verdict["scores"] & { followability: number };
  findings: (Omit<Finding, "about"> & { about: Finding["about"] | "followability" })[];
  /** The paper's argument as the blind reader retold it. */
  retelling: string;
}

/** One review round, as the job returns it (docs/integrations.md, `reviews`). */
export interface Review extends Judgement {
  round: number;
  passed: boolean;
}

/** The pass rule: every score at least 4 and every check passed. */
export const passes = (v: Judgement): boolean => Object.values(v.scores).every((s) => s >= PASS_SCORE) && Object.values(v.checks).every(Boolean);

/** What a failing review failed on, as the read's `review` note names it. */
export function failings(v: Judgement): string {
  const low = SCORES.filter((name) => v.scores[name] < PASS_SCORE).map((name) => `${name} ${v.scores[name]}`);
  const failed = REVIEW_CHECKS.filter((name) => !v.checks[name]).map((name) => name.replace("_", " "));
  return [low.length ? `scores below ${PASS_SCORE}: ${low.join(", ")}` : "", failed.length ? `checks failed: ${failed.join(", ")}` : ""].filter(Boolean).join("; ");
}

const TOOL = { name: "review", description: "Give the review's verdict: the four scores, the structure checks, then the findings.", parameters: VerdictSchema };
/** A judge is asked again this many times when its answer is no valid verdict. */
const REPAIRS = 1;

// The reviewer reads the whole paper where the model's context has room for it: the read cites the appendix, which
// the writer read through section(). The room is the context less what the verdict and its reasoning, the prompt and
// the figures take, at a low count of characters per token (LaTeX and numbers run short), less the read itself.
const CHARS_PER_TOKEN = 3;
const RESERVED_TOKENS = 60_000;
const IMAGE_TOKENS = 2_000;
/** The context taken for a model that does not say its own. */
const DEFAULT_CONTEXT_TOKENS = 200_000;
/** The reviewer is given at least this much of the paper, whatever the sum says. */
const MIN_PAPER_CHARS = 10_000;

/** How many characters of the paper's text the reviewer's call has room for. */
export function paperRoom(contextWindow: number | undefined, readChars: number, images: number): number {
  const tokens = (contextWindow || DEFAULT_CONTEXT_TOKENS) - RESERVED_TOKENS - images * IMAGE_TOKENS;
  return Math.max(MIN_PAPER_CHARS, tokens * CHARS_PER_TOKEN - readChars);
}

export interface ReviewInput {
  paper: PaperInput;
  read: Read;
  selections: SelectionContext[];
  /** The figures the read cites for values read off a plot, each as the writer was shown it, with the paper's
   * number for it where the session has one ("Figure 6"). */
  plots: { name: string; number?: string; shot: Shot }[];
  /** The places the read cites that code could not look up in the paper's text. */
  unverified: string[];
  /** The places and numbers code refused that the writer stood by. */
  stood: string[];
  /** The read's length against the paper's, which sets the length check. */
  length?: Length;
}

/** What a judge's call came to: its verdict, or why there is none, and the calls made. */
export interface Asked<T> {
  verdict?: T;
  error?: string;
  /** Each model call made, for the run's usage and trace. */
  calls: AssistantMessage[];
}

/** Ask a judge (the reviewer, the blind reader) for a verdict through its one tool; an answer that is no valid
 * verdict is sent back with what is wrong with it, once. A call that throws (the sign-in gone) ends it like one
 * answered with an error, with the calls made before it. */
export async function ask<T>(
  models: Models,
  model: Model<string>,
  call: { who: string; systemPrompt: string; tool: { name: string; description: string; parameters: TSchema }; content: (TextContent | ImageContent)[] },
  opts: { effort: ThinkingLevel; signal?: AbortSignal; onPayload?: (payload: unknown) => unknown },
): Promise<Asked<T>> {
  const { tool } = call;
  const messages: Message[] = [{ role: "user", content: call.content, timestamp: Date.now() }];
  const calls: AssistantMessage[] = [];
  for (let attempt = 0; ; attempt++) {
    let answer: AssistantMessage;
    try {
      answer = await models.completeSimple(model, { systemPrompt: call.systemPrompt, tools: [tool], messages }, { reasoning: opts.effort, signal: opts.signal, onPayload: opts.onPayload });
    } catch (e) {
      return { error: e instanceof Error ? e.message : String(e), calls };
    }
    calls.push(answer);
    if (answer.stopReason === "error" || answer.stopReason === "aborted") return { error: answer.errorMessage ?? answer.stopReason, calls };
    const used = answer.content.find((c) => c.type === "toolCall" && c.name === tool.name);
    const errors = used?.type === "toolCall" ? schemaErrors(tool.parameters, used.arguments) : [`no call of the ${tool.name} tool`];
    if (used?.type === "toolCall" && errors.length === 0) return { verdict: used.arguments as T, calls };
    if (attempt >= REPAIRS) return { error: `${call.who} gave no valid verdict: ${errors.slice(0, 5).join("; ")}`, calls };
    messages.push(answer);
    if (used?.type === "toolCall")
      messages.push({ role: "toolResult", toolCallId: used.id, toolName: used.name, content: [{ type: "text", text: `The verdict does not fit the tool's schema:\n${errors.join("\n")}\nCall ${tool.name} again with a valid verdict.` }], isError: true, timestamp: Date.now() });
    else messages.push({ role: "user", content: `Give your verdict by calling the ${tool.name} tool.`, timestamp: Date.now() });
  }
}

export interface ReviewOutcome extends Asked<Verdict> {
  /** What of the paper's text the reviewer was not given, for want of room in the model's context; empty when it
   * read the whole. */
  left: string[];
}

/** Ask the reviewer for its verdict on the read, against the paper. */
export async function review(
  models: Models,
  model: Model<string>,
  input: ReviewInput,
  opts: { effort: ThinkingLevel; signal?: AbortSignal; onPayload?: (payload: unknown) => unknown },
): Promise<ReviewOutcome> {
  const { text, left } = reviewerText(input.paper, paperRoom(model.contextWindow, JSON.stringify(input.read).length, input.plots.length));
  const content: (TextContent | ImageContent)[] = [{ type: "text", text: reviewerMessage(input.paper, text, left, input.read, input.unverified, input.stood, input.length) }];
  for (const { name, number, shot } of input.plots) {
    // the number is the writer's word for which figure this is, not something code confirmed
    const what = `The writer's figure "${name}"${number ? `, made as the paper's ${number}` : ""}, which the read gives values read off:`;
    content.push({ type: "text", text: what }, { type: "image", data: shot.data, mimeType: shot.mimeType });
  }
  const asked = await ask<Verdict>(models, model, { who: "the reviewer", systemPrompt: reviewerPrompt(input.selections), tool: TOOL, content }, opts);
  // the length check is code's, from the measure, not the reviewer's estimate
  if (asked.verdict && input.length) asked.verdict.checks.length = input.length.share <= LENGTH_MAX;
  return { ...asked, left };
}
