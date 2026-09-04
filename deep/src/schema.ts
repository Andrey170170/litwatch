/**
 * The reading card: what `submit` must receive. The schema is the checklist, so completeness
 * is enforced here rather than by a second model pass.
 */

import { Type, type Static } from "typebox";
import { Value } from "typebox/value";

const enumOf = <T extends string>(values: readonly T[], description: string) =>
  Type.Union(
    values.map((v) => Type.Literal(v)),
    { description },
  );

export const PAPER_KINDS = ["method", "analysis", "benchmark", "position"] as const;
export const MONITOR_TYPES = ["white-box", "black-box", "hybrid", "not applicable"] as const;
export const INTERNALS_SIGNALS = [
  "probes",
  "sparse autoencoders",
  "attention",
  "activation patching",
  "steering",
  "other",
] as const;
export const THREAT_MODELS = [
  "post-hoc rationalization",
  "hidden or encoded reasoning",
  "hint non-acknowledgement",
  "sandbagging",
  "other",
] as const;

export const ResultSchema = Type.Object(
  {
    summary: Type.String({
      minLength: 300,
      maxLength: 2500,
      description:
        "Two to four paragraphs for lab members who have not read the paper: the problem, the method, " +
        "the main results with numbers, and what is new. Plain prose, no markdown.",
    }),
    key_claim: Type.String({
      minLength: 30,
      maxLength: 400,
      description: "The paper's central claim in one sentence.",
    }),
    paper_kind: enumOf(PAPER_KINDS, "What kind of contribution the paper mainly makes."),
    monitor_type: enumOf(
      MONITOR_TYPES,
      "How the CoT is monitored or evaluated: from internals (white-box), from text only (black-box), " +
        "both (hybrid), or not applicable when the paper monitors nothing.",
    ),
    internals_signal: Type.Array(enumOf(INTERNALS_SIGNALS, "Model-internal signal used."), {
      description: "Empty when the paper uses no model internals.",
    }),
    threat_model: Type.Array(
      enumOf(THREAT_MODELS, "The kind of unfaithfulness or hidden reasoning the paper is about."),
      { minItems: 1 },
    ),
    faithfulness_definition: Type.String({
      minLength: 80,
      maxLength: 1500,
      description:
        "How faithfulness (or the monitored property) is defined and operationalized, and how ground " +
        "truth is constructed. If the paper defines none, say so and say what it measures instead.",
    }),
    evaluation_setup: Type.Object({
      models: Type.Array(Type.String(), { description: "Exact model names evaluated." }),
      tasks: Type.Array(Type.String(), { description: "Datasets or tasks used." }),
      generalization: Type.String({
        minLength: 20,
        description:
          "Whether generalization across tasks or models was tested, and what was found.",
      }),
    }),
    prior_work: Type.Array(
      Type.Object({
        title: Type.String({ minLength: 5 }),
        relation: Type.String({
          minLength: 20,
          description: "What this paper takes from it or claims to improve on.",
        }),
        arxiv_id: Type.Optional(Type.String({ pattern: "^\\d{4}\\.\\d{4,5}$" })),
      }),
      {
        minItems: 1,
        maxItems: 6,
        description: "The closest prior work the paper builds on or claims to improve on.",
      },
    ),
    code_available: Type.Boolean({
      description: "True only when a repository or artifact link was found and reachable.",
    }),
    code_url: Type.Optional(Type.String({ format: "uri" })),
    weights_available: Type.Boolean({
      description: "True when trained artifacts (probes, SAEs, fine-tuned weights) are released.",
    }),
    limitations: Type.Array(Type.String({ minLength: 20 }), {
      minItems: 1,
      maxItems: 8,
      description: "Concrete limitations, the paper's own and ones you see.",
    }),
  },
  { additionalProperties: false },
);

export type Result = Static<typeof ResultSchema>;

export type Validation = { ok: true; value: Result } | { ok: false; errors: string[] };

export function validate(input: unknown): Validation {
  const errors = schemaErrors(input);
  if (errors.length === 0) {
    const value = input as Result;
    if (value.monitor_type !== "black-box" && value.monitor_type !== "not applicable") {
      if (value.internals_signal.length === 0)
        errors.push("/internals_signal: must name the internal signal for a white-box or hybrid monitor");
    }
    if (value.code_available && !value.code_url)
      errors.push("/code_url: required when code_available is true");
  }
  return errors.length ? { ok: false, errors } : { ok: true, value: input as Result };
}

/** One line per offending path; union noise from literal alternatives collapsed into one line. */
function schemaErrors(input: unknown): string[] {
  const byPath = new Map<string, string[]>();
  const allowed = new Map<string, string[]>();
  for (const e of Value.Errors(ResultSchema, input)) {
    const path = e.instancePath || "/";
    if (e.keyword === "const") {
      const list = allowed.get(path) ?? [];
      list.push(String((e.params as { allowedValue?: unknown }).allowedValue));
      allowed.set(path, list);
      continue;
    }
    if (e.keyword === "anyOf") continue;
    const list = byPath.get(path) ?? [];
    list.push(e.message ?? e.keyword);
    byPath.set(path, list);
  }
  for (const [path, values] of allowed) byPath.set(path, [`must be one of: ${values.join(", ")}`]);
  return [...byPath].map(([path, msgs]) => `${path}: ${[...new Set(msgs)].join("; ")}`);
}

/** The schema as the model sees it in the prompt. */
export const SCHEMA_JSON = JSON.stringify(ResultSchema, null, 1);
