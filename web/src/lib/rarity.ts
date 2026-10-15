/** Rarity, grades, Fit and Weight as the pages name and colour them, and the shelves a list is cut into. */
import type { Schemas } from "@/api/client";

export type Rarity = Schemas["Rarity"];
export type Grade = Schemas["Grade"];
export type GradeLevel = Schemas["GradeLevel"];
export type Fit = Schemas["Fit"];
export type Weight = Schemas["Weight"];
export type Depth = Schemas["Depth"];

/** High to low, the order the rarity bar and the shelves use. */
export const RARITIES: Rarity[] = ["legendary", "epic", "rare", "uncommon", "common"];

export const RARITY_NAME: Record<Rarity, string> = {
  legendary: "Legendary",
  epic: "Epic",
  rare: "Rare",
  uncommon: "Uncommon",
  common: "Common",
};
export const GRADE_NAME: Record<Grade, string> = { must_read: "must read", relevant: "relevant", adjacent: "adjacent", none: "none" };
export const WEIGHT_NAME: Record<Weight, string> = { landmark: "landmark", solid: "solid", incremental: "incremental" };
export const DEPTH_NAME: Record<Depth, string> = { focus: "Focus", area: "Area", field: "Field" };
export const CADENCE: Record<Depth, string> = { focus: "daily and weekly digests", area: "daily and weekly digests", field: "weekly digest" };
export const CONTRIBUTION_NAME: Record<Schemas["Contribution"], string> = {
  method: "Method",
  analysis: "Analysis",
  benchmark: "Benchmark",
  theory: "Theory",
  position: "Position",
  survey: "Survey",
};

/** The CSS colour of a rarity, or the line colour for none. */
export const rc = (r: Rarity | null | undefined): string => (r ? `var(--r-${r})` : "var(--line)");

/** The rarity a Weight alone gives, the floor an ungraded paper has in a mixed list (pipeline.md, section 5). */
export function weightFloor(w: Weight | null | undefined): Rarity {
  return w === "landmark" ? "rare" : w === "solid" ? "uncommon" : "common";
}

/** "must read · landmark" for a topic row; the grade alone otherwise. */
export function gradeLine(grade: Grade | null | undefined, weight: Weight | null | undefined): string {
  if (!grade) return "ungraded";
  return grade === "must_read" && weight === "landmark" ? "must read · landmark" : GRADE_NAME[grade];
}

/**
 * A shelf: which papers it holds (a filter on the list's endpoint), its head's colour, its form (reading rows with
 * Gists, or one-line table rows), and whether it starts open or folded to a count.
 */
export type Shelf = {
  key: string;
  name: string;
  colour: Rarity;
  form: "reading" | "table";
  open: boolean;
  rarity?: Rarity[];
  grade?: GradeLevel[];
};

/** Papers: shelves by mixed rarity. */
export const PAPER_SHELVES: Shelf[] = [
  { key: "top", name: "Epic and legendary", colour: "legendary", form: "reading", open: true, rarity: ["epic", "legendary"] },
  { key: "rare", name: "Rare", colour: "rare", form: "table", open: true, rarity: ["rare"] },
  { key: "uncommon", name: "Uncommon", colour: "uncommon", form: "table", open: false, rarity: ["uncommon"] },
  { key: "common", name: "Common", colour: "common", form: "table", open: false, rarity: ["common"] },
];

/** A topic page: shelves by the topic's grade. */
export const TOPIC_SHELVES: Shelf[] = [
  { key: "must_read", name: "Must read", colour: "legendary", form: "reading", open: true, grade: ["must_read"] },
  { key: "relevant", name: "Relevant", colour: "rare", form: "table", open: true, grade: ["relevant"] },
  { key: "adjacent", name: "Adjacent", colour: "uncommon", form: "table", open: false, grade: ["adjacent"] },
  { key: "rest", name: "In the topic, not graded up", colour: "common", form: "table", open: false, grade: ["none", "ungraded"] },
];

/** The shelves a rarity filter leaves: on Papers those holding a chosen level; a topic page keeps every shelf,
 * since an ungraded paper's colour is its Weight's and could sit anywhere. */
export function shelvesFor(shelves: Shelf[], rarity: Rarity[]): Shelf[] {
  if (!rarity.length) return shelves;
  return shelves.filter((s) => !s.rarity || s.rarity.some((r) => rarity.includes(r)));
}

/** The rarity bar's segment sizes: each level's share, with a floor so every level stays tappable. */
export function segmentFlex(counts: Record<Rarity, number>): Record<Rarity, number> {
  const total = RARITIES.reduce((a, r) => a + counts[r], 0) || 1;
  return Object.fromEntries(RARITIES.map((r) => [r, Math.max(counts[r] / total, 0.07)])) as Record<Rarity, number>;
}
