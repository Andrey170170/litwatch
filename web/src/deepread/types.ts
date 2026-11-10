/**
 * The Deep read's types, by name. `read.d.ts` is generated from the schema the deep service owns
 * (`deep/src/read.ts`, exported by `npm run read-schema` there, then `pnpm generate:read` here); these only name
 * its parts.
 */
import type { CSSProperties } from "react";
import type { Read } from "./read";

export type { Read };
export type Section = Read["sections"][number];
export type SectionKind = Section["kind"];
export type SectionOf<K extends SectionKind> = Extract<Section, { kind: K }>;
export type Block = Extract<Section, { blocks: unknown }>["blocks"][number];
export type BlockOf<T extends Block["type"]> = Extract<Block, { type: T }>;
export type Claim = SectionOf<"claims">["claims"][number];
export type Fact = SectionOf<"glance">["facts"][number];
export type Colour = NonNullable<BlockOf<"diagram">["nodes"][number]["colour"]>;

/** Whether a stored card is a Deep read: a Deep card from before carries no format. */
export function isRead(card: unknown): card is Read {
  return typeof card === "object" && card !== null && (card as { format?: unknown }).format === "read";
}

/** The schema's colour names as the app's tokens. The accent is too pale to draw with on a light page. */
export const COLOUR: Record<Colour, string> = {
  neutral: "var(--fg3)",
  accent: "light-dark(#b3a900, var(--acc))",
  red: "var(--sig)",
  orange: "var(--r-legendary)",
  amber: "var(--warn)",
  green: "var(--r-uncommon)",
  cyan: "var(--cy)",
  blue: "var(--r-rare)",
  purple: "var(--r-epic)",
};

/** What marks a section's element with its colour: its kind, which the stylesheet colours, and for a lens its
 * topic's colour. A lens whose topic is unknown here (unfollowed since) stays neutral. */
export function sectionMark(section: Section, topicColour: (slug: string) => string | undefined): { "data-k": SectionKind; style?: CSSProperties } {
  const colour = section.kind === "lens" ? topicColour(section.topic) : undefined;
  return { "data-k": section.kind, style: colour ? ({ "--tc": colour } as CSSProperties) : undefined };
}
