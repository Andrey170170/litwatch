/** Topic colours and the words for kinds and cadences. */
import type { TopicCard, TopicKind } from "@/api/client";
import { fmt } from "./format";

export const PAUSED_COLOUR = "#9a9a94";

export function topicColour(colour: string | null | undefined, status?: string): string {
  if (status && status !== "active") return PAUSED_COLOUR;
  return colour || "var(--foreground)";
}

export const KIND_LABEL: Record<TopicKind, string> = { watch: "Watch", radar: "Radar", pulse: "Pulse" };

export const KIND_ORDER: TopicKind[] = ["watch", "radar", "pulse"];

/** How often a topic is written up: nothing for Watch (graded nightly), else its cadence. */
export function cadence(c: Pick<TopicCard, "kind" | "cadence">): string | null {
  if (c.kind === "watch") return null;
  return c.cadence === "daily" ? "daily" : "weekly";
}

/** "Reviewed Monday", or "Reviewed 6 October" when older than a week; null when never. */
export function lastReview(c: Pick<TopicCard, "last_review">, today = new Date()): string | null {
  if (!c.last_review) return null;
  const age = (today.getTime() - new Date(c.last_review).getTime()) / 86400000;
  return `Reviewed ${age < 6 ? fmt.weekday(c.last_review) : fmt.published(c.last_review)}`;
}
