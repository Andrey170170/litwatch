/**
 * A deep card, as the deep service wrote it: a JSON object whose shape depends on the topic's
 * extension. Rendered as prose in the reading column: the summary first, then the fields the
 * card has, in a sensible order, each as paragraphs, a list, or a small block of fields.
 */
import type { DeepCard as DeepCardT } from "@/api/client";
import { fmt } from "@/lib/format";
import { modelName } from "@/components/PaperView";

/** The fields a card usually has, in reading order; anything else follows alphabetically. */
const ORDER = ["summary", "key_claim", "paper_kind", "evaluation_setup", "limitations", "prior_work"];
/** Yes/no facts shown as one meta line instead of sections. */
const FACTS = ["code_available", "weights_available"];

export function DeepCardView({ card }: { card: DeepCardT }) {
  const body = card.card ?? {};
  const keys = Object.keys(body).filter((k) => !FACTS.includes(k) && present(body[k]));
  keys.sort((a, b) => rank(a) - rank(b) || a.localeCompare(b));
  const facts = FACTS.filter((k) => body[k] !== undefined && body[k] !== null).map((k) => `${label(k)}: ${yesNo(body[k])}`);
  return (
    <div className="deep-card">
      <p className="meta mb-2">
        {[card.model ? `by ${modelName(card.model)}` : null, card.written_at ? fmt.published(card.written_at) : null, ...facts].filter(Boolean).join(" · ")}
      </p>
      {keys.map((k) => (
        <section key={k} className="mt-3 first:mt-0">
          {k !== "summary" && <h4 className="mb-1 text-[12.5px] font-semibold tracking-[.02em] text-muted-foreground">{label(k)}</h4>}
          <Value value={body[k]} />
        </section>
      ))}
    </div>
  );
}

function rank(k: string) {
  const i = ORDER.indexOf(k);
  return i === -1 ? ORDER.length : i;
}

function present(v: unknown): boolean {
  if (v === null || v === undefined || v === "") return false;
  if (Array.isArray(v)) return v.length > 0;
  if (typeof v === "object") return Object.values(v as object).some(present);
  return true;
}

/** "evaluation_setup" → "Evaluation setup". */
export function label(k: string): string {
  const words = k.replace(/[_-]+/g, " ").trim();
  return words.charAt(0).toUpperCase() + words.slice(1);
}

function yesNo(v: unknown): string {
  return typeof v === "boolean" ? (v ? "yes" : "no") : String(v);
}

/** Strings short enough to run together as a comma list (tasks, models, targets); anything
 * sentence-like (limitations, caveats) reads better as a list of its own. */
function terse(items: string[]): boolean {
  return items.every((s) => {
    const t = s.trim();
    return t.length <= 60 && !/[.!?]$/.test(t) && !/[.;:!?]\s/.test(t);
  });
}

/** A value as prose: paragraphs for text, a list for arrays, labelled lines for objects. */
function Value({ value, depth = 0 }: { value: unknown; depth?: number }) {
  if (typeof value === "string")
    return (
      <div className="font-serif text-[15.5px] leading-[1.5] text-body [&>p+p]:mt-2.5">
        {value
          .split(/\n\s*\n/)
          .map((p) => p.trim())
          .filter(Boolean)
          .map((p, i) => (
            <p key={i}>{p}</p>
          ))}
      </div>
    );
  if (typeof value === "number" || typeof value === "boolean") return <p className="text-[15px] text-body">{yesNo(value)}</p>;
  if (Array.isArray(value)) {
    if (value.every((v) => typeof v === "string") && terse(value as string[]))
      return <p className="text-[15px] text-body">{(value as string[]).join(", ")}</p>;
    return (
      <ul className="space-y-2">
        {value.map((v, i) => (
          <li key={i} className="border-l-2 border-border pl-3">
            <Value value={v} depth={depth + 1} />
          </li>
        ))}
      </ul>
    );
  }
  if (value && typeof value === "object") {
    const entries = Object.entries(value as Record<string, unknown>).filter(([, v]) => present(v));
    // A titled item (a prior-work entry) leads with its title.
    const title = entries.find(([k]) => k === "title")?.[1];
    return (
      <div className="space-y-1.5">
        {typeof title === "string" && <p className="font-serif text-[15px] font-semibold leading-[1.3]">{title}</p>}
        {entries
          .filter(([k]) => k !== "title")
          .map(([k, v]) => (
            <div key={k} className="text-[14px] text-body">
              <span className="text-muted-foreground">{label(k)}: </span>
              {typeof v === "string" || typeof v === "number" || typeof v === "boolean" ? (
                yesNo(v)
              ) : Array.isArray(v) && v.every((x) => typeof x === "string") && terse(v as string[]) ? (
                (v as string[]).join(", ")
              ) : (
                <div className="mt-1 pl-3">
                  <Value value={v} depth={depth + 1} />
                </div>
              )}
            </div>
          ))}
      </div>
    );
  }
  return null;
}
