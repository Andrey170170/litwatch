/**
 * What the Deep read (`read.ts`) keeps from the Deep card before it, in the card's shapes, so what reads them from a
 * stored card reads them from a read: the prior work, the collection's papers, the code link and the process notes.
 * Also how a schema's errors are worded for the model, and the rule on which collection papers a read may name.
 */

import { Type, type TSchema, type TUnsafe } from "typebox";
import { Settings } from "typebox/system";
import { Value } from "typebox/value";

// TypeBox stops collecting after 8 raw errors by default, and each enum burns one per literal; the
// model needs the whole list to fix a rejected submit in one round.
Settings.Set({ maxErrors: 256 });

/** One of a list of strings. Typed as the union of them: TypeBox's own union of an array of literals has no
 * static type. */
export const enumOf = <T extends string>(values: readonly T[], description: string) =>
  Type.Union(
    values.map((v) => Type.Literal(v)),
    { description },
  ) as unknown as TUnsafe<T>;

export const RELATIONS = ["agrees", "contradicts", "extends", "supersedes"] as const;
export const NOTE_KINDS = ["access", "tool", "budget", "input"] as const;
/** The model's process notes; the service adds its own after validation, which this does not count. */
export const MAX_MODEL_NOTES = 6;
// A version-stripped arXiv id, modern (2305.04388) or legacy (cs/9901001, math.AG/0601001), as
// litwatch stores them.
const ARXIV_ID = "^(?:\\d{4}\\.\\d{4,5}|[a-z-]+(?:\\.[A-Z]{2})?/\\d{7})$";

export const PRIOR_WORK = Type.Array(
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
);

export const AMONG_COLLECTION = Type.Array(
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
);

export const CODE_URL = Type.Optional(Type.String({ format: "uri" }));

/** The process notes the model may write: its own source only, and none of the service's kinds. */
export const MODEL_NOTES = Type.Optional(
  Type.Array(
    Type.Object(
      {
        kind: enumOf(
          NOTE_KINDS,
          "What held you back: access (a page or repository you could not reach), tool (a tool that failed " +
            "or could not do it), budget (cells or fetches ran out), input (the paper's text was cut or unreadable).",
        ),
        source: Type.Literal("model"),
        note: Type.String({
          minLength: 20,
          maxLength: 400,
          description: "One sentence: what you could not do or check, and what the read says because of it.",
        }),
        url: Type.Optional(Type.String({ pattern: "^https?://\\S+$", description: "The page concerned, an absolute http(s) URL." })),
      },
      { additionalProperties: false },
    ),
    {
      maxItems: MAX_MODEL_NOTES,
      description:
        "What you could not do because of your tools, access or budget, not what the paper lacks (that is a " +
        "limitation). Leave it out when nothing held you back.",
    },
  ),
);

/** Whether a URL is an absolute http(s) one. */
export const isWebUrl = (url: string): boolean => {
  try {
    const u = new URL(url);
    return u.protocol === "https:" || u.protocol === "http:";
  } catch {
    return false;
  }
};

/** What the session's searches found: the collection's papers the agent was shown, and the paper itself. */
export interface Found {
  collection: ReadonlySet<string>;
  self: string;
}

/** Each among_collection entry must be a collection paper a search in this session returned, other than the
 * paper itself. */
export function amongErrors(among: readonly { arxiv_id: string }[], found: Found): string[] {
  return among.flatMap((entry, i) =>
    entry.arxiv_id === found.self
      ? [`/among_collection/${i}/arxiv_id: is this paper itself`]
      : found.collection.has(entry.arxiv_id)
        ? []
        : [`/among_collection/${i}/arxiv_id: no store.search in this session returned it with in_collection true`],
  );
}

/** One line per offending path; union noise from literal alternatives collapsed into one line. */
function leafErrors(schema: TSchema, input: unknown, at: string): string[] {
  const byPath = new Map<string, string[]>();
  const allowed = new Map<string, string[]>();
  for (const e of Value.Errors(schema, input)) {
    const path = at + e.instancePath || "/";
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
  for (const [path, values] of allowed) if (!byPath.has(path)) byPath.set(path, [`must be one of: ${[...new Set(values)].join(", ")}`]);
  return [...byPath].map(([path, msgs]) => `${path}: ${[...new Set(msgs)].join("; ")}`);
}

type Shape = TSchema & {
  type?: string;
  anyOf?: Shape[];
  properties?: Record<string, Shape>;
  required?: string[];
  additionalProperties?: boolean;
  items?: Shape | Shape[];
  const?: unknown;
};
const isObject = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null && !Array.isArray(v);
/** The property that tells a union's object variants apart: each variant requires it, as a literal or literals. */
function discriminator(variants: Shape[]): string | undefined {
  if (!variants.every((v) => v.type === "object" && v.properties)) return undefined;
  return ["type", "kind"].find((name) => variants.every((v) => v.properties![name] && v.required?.includes(name)));
}

/**
 * A value's errors against a schema, one line per offending path, worded for the model that must fix them. A
 * union of objects told apart by `type` or `kind` (a read's blocks and sections) is checked against the one
 * variant the value names, so a wrong field in a table is reported as that, not as a failure to be each other
 * block in turn.
 */
export function schemaErrors(schema: TSchema, input: unknown, at = ""): string[] {
  const shape = schema as Shape;
  if (shape.anyOf) {
    const name = discriminator(shape.anyOf);
    if (name === undefined) {
      // no literal tells them apart (a table's row): the one object variant whose required fields are all there
      const fitting = isObject(input) ? shape.anyOf.filter((v) => v.type === "object" && v.properties && (v.required ?? []).every((r) => r in input)) : [];
      return fitting.length === 1 ? schemaErrors(fitting[0]!, input, at) : leafErrors(schema, input, at);
    }
    if (!isObject(input)) return [`${at || "/"}: must be an object`];
    const variant = shape.anyOf.find((v) => Value.Check(v.properties![name]!, input[name]));
    if (variant) return schemaErrors(variant, input, at);
    const names = shape.anyOf.flatMap((v) => {
      const p = v.properties![name]!;
      return p.anyOf ? p.anyOf.map((l) => String(l.const)) : [String(p.const)];
    });
    return [`${at}/${name}: must be one of: ${[...new Set(names)].join(", ")}`];
  }
  if (shape.type === "object" && shape.properties) {
    if (!isObject(input)) return [`${at || "/"}: must be an object`];
    const errors: string[] = [];
    for (const name of shape.required ?? []) if (!(name in input)) errors.push(`${at}/${name}: is required`);
    for (const [name, value] of Object.entries(input)) {
      const property = shape.properties[name];
      if (property) errors.push(...schemaErrors(property, value, `${at}/${name}`));
      else if (shape.additionalProperties === false) errors.push(`${at}/${name}: is not a field here`);
    }
    return errors;
  }
  if (shape.type === "array" && shape.items && !Array.isArray(shape.items) && Array.isArray(input)) {
    // the array's own limits, then each item against the item schema
    const own = leafErrors({ ...shape, items: {} } as TSchema, input, at);
    return [...own, ...input.flatMap((item, i) => schemaErrors(shape.items as Shape, item, `${at}/${i}`))];
  }
  return leafErrors(schema, input, at);
}
