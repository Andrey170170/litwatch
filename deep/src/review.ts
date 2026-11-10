/**
 * The reviewer (docs/specs/deep-read.md, "A reviewer"): one call on the writer's model, with no session, that reads
 * the read against the paper and the rubric and answers through a single `review` tool. Its answer is validated
 * against the tool's schema; the pass rule is the service's, not the model's: every score at least 4 and every
 * check passed.
 */

import type { AssistantMessage, ImageContent, Message, Model, Models, TextContent, ThinkingLevel } from "@earendil-works/pi-ai";
import { Type, type Static } from "typebox";
import type { Shot } from "./imaging.ts";
import { reviewerMessage, reviewerPrompt, reviewerText, type PaperInput, type SelectionContext } from "./prompt.ts";
import type { Read } from "./read.ts";
import { enumOf, schemaErrors } from "./schema.ts";

export const SCORES = ["faithfulness", "clarity", "depth", "calibration"] as const;
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
        clarity: score("A lab member follows it without the paper; free of the three prose faults."),
        depth: score("Method and results close to the paper's detail."),
        calibration: score("Verdicts and CARD notes fair to the paper."),
      },
      { additionalProperties: false },
    ),
    checks: Type.Object(
      {
        anchors: Type.Boolean({ description: "The sections in their places, each holding what its name says." }),
        experiments: Type.Boolean({ description: "Findings given as experiments." }),
        why_boxes: Type.Boolean({ description: "The paper's reasons in WHY boxes at the choices they explain." }),
        voice: Type.Boolean({ description: "Objective: no reader addressed, no audience, no advice to read, no hype." }),
        length: Type.Boolean({ description: "In proportion to the paper." }),
      },
      { additionalProperties: false, description: "True: passed." },
    ),
    findings: Type.Array(
      Type.Object(
        {
          about: enumOf([...SCORES, ...REVIEW_CHECKS] as const, "The score or check the finding counts against."),
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

export type Verdict = Static<typeof VerdictSchema>;

/** One review round, as the job returns it (docs/integrations.md, `reviews`). */
export interface Review extends Verdict {
  round: number;
  passed: boolean;
}

/** The pass rule: every score at least 4 and every check passed. */
export const passes = (v: Verdict): boolean => Object.values(v.scores).every((s) => s >= PASS_SCORE) && Object.values(v.checks).every(Boolean);

/** What a failing review failed on, as the read's `review` note names it. */
export function failings(v: Verdict): string {
  const low = SCORES.filter((name) => v.scores[name] < PASS_SCORE).map((name) => `${name} ${v.scores[name]}`);
  const failed = REVIEW_CHECKS.filter((name) => !v.checks[name]).map((name) => name.replace("_", " "));
  return [low.length ? `scores below ${PASS_SCORE}: ${low.join(", ")}` : "", failed.length ? `checks failed: ${failed.join(", ")}` : ""].filter(Boolean).join("; ");
}

const TOOL = { name: "review", description: "Give the review's verdict: the four scores, the structure checks, then the findings.", parameters: VerdictSchema };
/** The reviewer is asked again this many times when its answer is no valid verdict. */
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
}

export interface ReviewOutcome {
  /** The verdict, or undefined when the reviewer gave none. */
  verdict?: Verdict;
  /** Why there is no verdict. */
  error?: string;
  /** Each model call made, for the run's usage and trace. */
  calls: AssistantMessage[];
  /** What of the paper's text the reviewer was not given, for want of room in the model's context; empty when it
   * read the whole. */
  left: string[];
}

/** Ask the reviewer once; an answer that is no valid verdict is sent back with what is wrong with it, once. A call
 * that throws (the sign-in gone) ends the review like one answered with an error, with the calls made before it. */
export async function review(
  models: Models,
  model: Model<string>,
  input: ReviewInput,
  opts: { effort: ThinkingLevel; signal?: AbortSignal; onPayload?: (payload: unknown) => unknown },
): Promise<ReviewOutcome> {
  const { text, left } = reviewerText(input.paper, paperRoom(model.contextWindow, JSON.stringify(input.read).length, input.plots.length));
  const content: (TextContent | ImageContent)[] = [{ type: "text", text: reviewerMessage(input.paper, text, left, input.read, input.unverified, input.stood) }];
  for (const { name, number, shot } of input.plots) {
    // the number is the writer's word for which figure this is, not something code confirmed
    const what = `The writer's figure "${name}"${number ? `, made as the paper's ${number}` : ""}, which the read gives values read off:`;
    content.push({ type: "text", text: what }, { type: "image", data: shot.data, mimeType: shot.mimeType });
  }
  const messages: Message[] = [{ role: "user", content, timestamp: Date.now() }];
  const calls: AssistantMessage[] = [];
  for (let attempt = 0; ; attempt++) {
    let answer: AssistantMessage;
    try {
      answer = await models.completeSimple(
        model,
        { systemPrompt: reviewerPrompt(input.selections), tools: [TOOL], messages },
        { reasoning: opts.effort, signal: opts.signal, onPayload: opts.onPayload },
      );
    } catch (e) {
      return { error: e instanceof Error ? e.message : String(e), calls, left };
    }
    calls.push(answer);
    if (answer.stopReason === "error" || answer.stopReason === "aborted") return { error: answer.errorMessage ?? answer.stopReason, calls, left };
    const call = answer.content.find((c) => c.type === "toolCall" && c.name === TOOL.name);
    const errors = call?.type === "toolCall" ? schemaErrors(VerdictSchema, call.arguments) : ["no call of the review tool"];
    if (call?.type === "toolCall" && errors.length === 0) return { verdict: call.arguments as Verdict, calls, left };
    if (attempt >= REPAIRS) return { error: `the reviewer gave no valid verdict: ${errors.slice(0, 5).join("; ")}`, calls, left };
    messages.push(answer);
    if (call?.type === "toolCall")
      messages.push({ role: "toolResult", toolCallId: call.id, toolName: call.name, content: [{ type: "text", text: `The verdict does not fit the tool's schema:\n${errors.join("\n")}\nCall review again with a valid verdict.` }], isError: true, timestamp: Date.now() });
    else messages.push({ role: "user", content: "Give your verdict by calling the review tool.", timestamp: Date.now() });
  }
}
