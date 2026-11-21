/** The thumbs' reasons (feedback.md). */
import type { Schemas } from "@/api/client";

export type ReactionReason = Schemas["ReactionReason"];
export type ReactionValue = Schemas["ReactionValue"];

export const REASONS: Record<ReactionValue, { reason: ReactionReason; label: string }[]> = {
  down: [
    { reason: "wrong_topic", label: "Wrong topic" },
    { reason: "overrated", label: "Overrated" },
    { reason: "not_interesting", label: "Not interesting" },
    { reason: "already_knew", label: "Already knew it" },
  ],
  up: [
    { reason: "underrated", label: "Underrated" },
    { reason: "great_find", label: "Great find" },
  ],
};

export function reasonLabel(reason: ReactionReason | null | undefined): string {
  for (const list of Object.values(REASONS)) for (const r of list) if (r.reason === reason) return r.label;
  return "";
}

