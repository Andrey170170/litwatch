/**
 * The reading card: what `submit` must receive. A topic-neutral core plus named extensions that a
 * job requests per selection. The schema is the checklist, so completeness is enforced here rather
 * than by a second model pass.
 */

import { Type, type Static, type TObject } from "typebox";
import { Settings } from "typebox/system";
import { Value } from "typebox/value";

// TypeBox stops collecting after 8 raw errors by default, and each enum burns one per literal; the
// model needs the whole list to fix a rejected submit in one round.
Settings.Set({ maxErrors: 256 });

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
export const MISMATCHES = ["omission", "commission"] as const;
export const GROUND_TRUTHS = ["task construction", "internals", "meta-evaluation", "none"] as const;
export const MONITOR_TARGETS = [
  "reward hacking",
  "sabotage",
  "deception",
  "sandbagging",
  "unsafe intent",
  "evaluation awareness",
  "other",
] as const;
export const PRESSURES = ["none", "prompted evasion", "optimization against the monitor", "both"] as const;
export const RELATIONS = ["agrees", "contradicts", "extends", "supersedes"] as const;
// A version-stripped arXiv id, modern (2305.04388) or legacy (cs/9901001, math.AG/0601001), as
// litwatch stores them.
const ARXIV_ID = "^(?:\\d{4}\\.\\d{4,5}|[a-z-]+(?:\\.[A-Z]{2})?/\\d{7})$";

const CORE = {
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
  evaluation_setup: Type.Object({
    models: Type.Array(Type.String(), { description: "Exact model names evaluated." }),
    tasks: Type.Array(Type.String(), { description: "Datasets or tasks used." }),
    generalization: Type.String({
      minLength: 20,
      description: "Whether generalization across tasks or models was tested, and what was found.",
    }),
  }),
  prior_work: Type.Array(
    Type.Object({
      title: Type.String({ minLength: 5 }),
      relation: Type.String({
        minLength: 20,
        description: "What this paper takes from it or claims to improve on.",
      }),
      arxiv_id: Type.Optional(Type.String({ pattern: ARXIV_ID })),
    }),
    {
      minItems: 1,
      maxItems: 6,
      description: "The closest prior work the paper builds on or claims to improve on.",
    },
  ),
  among_collection: Type.Array(
    Type.Object({
      arxiv_id: Type.String({ pattern: ARXIV_ID, description: "As store.search returned it." }),
      title: Type.String({ minLength: 5 }),
      relation: enumOf(RELATIONS, "How this paper stands to that one."),
      note: Type.String({
        minLength: 20,
        maxLength: 400,
        description: "One sentence on where the two meet: the shared question and what differs.",
      }),
    }),
    {
      maxItems: 8,
      description:
        "Papers the lab's collection holds (store.search hits with in_collection true), recent and on the " +
        "same question, that this paper agrees with, contradicts, extends or supersedes. Only relations both " +
        "papers' texts support; empty when none does.",
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
};

export const CoreSchema = Type.Object(CORE, { additionalProperties: false });

/** Extension for the chain-of-thought faithfulness selection. */
export const FaithfulnessSchema = Type.Object(
  {
    mismatch: Type.Array(enumOf(MISMATCHES, "Kind of unfaithfulness."), {
      description:
        "Which kind of unfaithfulness the paper detects or measures, in BonaFide's terms: omission " +
        "(the trace lacks a computation the model performed) or commission (a step describes a process " +
        "that did not take place). Empty when the paper measures neither directly.",
    }),
    ground_truth: enumOf(
      GROUND_TRUTHS,
      "Where the knowledge of what the model actually computed comes from: task construction (hints, " +
        "cues, hidden directives whose use is revealed by the output), model internals read against the " +
        "trace, a meta-evaluation of such methods, or none (proxies only, such as importance or plausibility).",
    ),
    detection_method: Type.String({
      minLength: 80,
      maxLength: 1500,
      description:
        "How the mismatch is detected or how ground truth is constructed, concretely: the intervention, " +
        "the probe, the labelling pipeline, the metric.",
    }),
    definition: Type.String({
      minLength: 80,
      maxLength: 1500,
      description:
        "How the paper defines and operationalises faithfulness (or the property it monitors), and how " +
        "that relates to the definition that a step is faithful iff it accurately describes a process " +
        "that took place at some point in the model.",
    }),
    monitor_type: enumOf(
      MONITOR_TYPES,
      "How the CoT is monitored or evaluated: from internals (white-box), from text only (black-box), " +
        "both (hybrid), or not applicable when the paper monitors nothing.",
    ),
    internals_signal: Type.Array(enumOf(INTERNALS_SIGNALS, "Model-internal signal used."), {
      description: "Empty when the paper uses no model internals.",
    }),
    generality: Type.String({
      minLength: 40,
      maxLength: 800,
      description:
        "Whether the method or benchmark is meant to transfer across tasks and models, or is a " +
        "single-domain audit; what evidence of transfer exists.",
    }),
  },
  { additionalProperties: false },
);

export type Faithfulness = Static<typeof FaithfulnessSchema>;

/** Extension for the chain-of-thought monitoring selection: reading traces for oversight. */
export const MonitoringSchema = Type.Object(
  {
    target: Type.Array(enumOf(MONITOR_TARGETS, "Misbehavior the monitor is meant to catch."), {
      description:
        "What the paper detects or studies detection of in the trace. Empty when the paper studies " +
        "monitorability or legibility without a specific misbehavior.",
    }),
    monitor_type: enumOf(
      MONITOR_TYPES,
      "How the trace is read: from internals (white-box), from text only (black-box), both (hybrid), " +
        "or not applicable when the paper monitors nothing (a position paper, a legibility study).",
    ),
    internals_signal: Type.Array(enumOf(INTERNALS_SIGNALS, "Model-internal signal used."), {
      description: "Empty when the paper uses no model internals.",
    }),
    monitor_method: Type.String({
      minLength: 80,
      maxLength: 1500,
      description:
        "How the monitor works and how it was evaluated, concretely: the monitor model and what it " +
        "sees, the probe, the labelling of ground-truth misbehavior, the metric (detection rate, false " +
        "positives, catch rate at a budget).",
    }),
    pressure: enumOf(
      PRESSURES,
      "Whether the monitor was tested under pressure: none, prompted evasion (the model is told to hide " +
        "its reasoning), optimization against the monitor (training or RL with the monitor in the reward), " +
        "or both.",
    ),
    robustness: Type.String({
      minLength: 40,
      maxLength: 800,
      description:
        "What the paper finds erodes or preserves detection: obfuscation, training against the monitor, " +
        "model scale, trace length, legibility, monitor strength. What was not tested.",
    }),
    generality: Type.String({
      minLength: 40,
      maxLength: 800,
      description:
        "Whether the monitor or evaluation is meant to transfer across tasks and models, or is a " +
        "single-domain audit; what evidence of transfer exists.",
    }),
  },
  { additionalProperties: false },
);

export type Monitoring = Static<typeof MonitoringSchema>;

/** Every extension a job may request, by the key it occupies in the result. */
export const EXTENSIONS: Record<string, TObject> = {
  faithfulness: FaithfulnessSchema,
  monitoring: MonitoringSchema,
};

const needsSignal = (key: string, monitor_type: string, signals: unknown[]): string[] =>
  monitor_type !== "black-box" && monitor_type !== "not applicable" && signals.length === 0
    ? [`/${key}/internals_signal: must name the internal signal for a white-box or hybrid monitor`]
    : [];

/** Cross-field rules per extension; each returns error lines, empty when the value is consistent. */
const EXTENSION_RULES: Record<string, (value: unknown) => string[]> = {
  faithfulness: (value) => {
    const f = value as Faithfulness;
    return needsSignal("faithfulness", f.monitor_type, f.internals_signal);
  },
  monitoring: (value) => {
    const m = value as Monitoring;
    return needsSignal("monitoring", m.monitor_type, m.internals_signal);
  },
};

export type Result = Static<typeof CoreSchema> & { faithfulness?: Faithfulness; monitoring?: Monitoring };

/** The core with each requested extension as a required property. Throws on an unknown name. */
export function resultSchema(extensions: string[]): TObject {
  const props: Record<string, TObject> = {};
  for (const name of extensions) {
    const ext = EXTENSIONS[name];
    if (!ext) throw new Error(`unknown extension "${name}"; known: ${Object.keys(EXTENSIONS).join(", ")}`);
    props[name] = ext;
  }
  return Type.Object({ ...CORE, ...props }, { additionalProperties: false });
}

/** The schema as the model sees it in the prompt. */
export function schemaJson(extensions: string[]): string {
  return JSON.stringify(resultSchema(extensions), null, 1);
}

export type Validation = { ok: true; value: Result } | { ok: false; errors: string[] };

/** What the session's searches found: the collection's papers the agent was shown, and the paper itself. */
export interface Found {
  collection: ReadonlySet<string>;
  self: string;
}

/** Validate a submitted result. With `found`, each among_collection entry must be a collection paper a
 * search in this session returned, other than the paper itself. */
export function validate(input: unknown, extensions: string[] = [], found?: Found): Validation {
  const schema = resultSchema(extensions);
  const errors = schemaErrors(schema, input);
  if (input && typeof input === "object")
    for (const key of Object.keys(EXTENSIONS))
      if (key in input && !extensions.includes(key)) errors.push(`/${key}: extension not requested for this job`);
  if (errors.length === 0) {
    const value = input as Result & Record<string, unknown>;
    if (value.code_available && !value.code_url) errors.push("/code_url: required when code_available is true");
    for (const name of extensions) errors.push(...(EXTENSION_RULES[name]?.(value[name]) ?? []));
    if (found) errors.push(...amongErrors(value.among_collection, found));
  }
  return errors.length ? { ok: false, errors } : { ok: true, value: input as Result };
}

function amongErrors(among: Result["among_collection"], found: Found): string[] {
  return among.flatMap((entry, i) =>
    entry.arxiv_id === found.self
      ? [`/among_collection/${i}/arxiv_id: is this paper itself`]
      : found.collection.has(entry.arxiv_id)
        ? []
        : [`/among_collection/${i}/arxiv_id: no store.search in this session returned it with in_collection true`],
  );
}

/** One line per offending path; union noise from literal alternatives collapsed into one line. */
function schemaErrors(schema: TObject, input: unknown): string[] {
  const byPath = new Map<string, string[]>();
  const allowed = new Map<string, string[]>();
  for (const e of Value.Errors(schema, input)) {
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
