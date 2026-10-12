/** Topic colours and the words for kinds and cadences. */
import type { TopicKind } from "@/api/client";

export const PAUSED_COLOUR = "#9a9a94";

export function topicColour(colour: string | null | undefined, status?: string): string {
  if (status && status !== "active") return PAUSED_COLOUR;
  return colour || "var(--foreground)";
}

export const KIND_LABEL: Record<TopicKind, string> = { watch: "Watch", radar: "Radar", pulse: "Pulse" };

export const KIND_ORDER: TopicKind[] = ["watch", "radar", "pulse"];

export function cadence(kind: TopicKind): string | null {
  // The API does not carry a topic's cadence yet; Radar and Pulse are weekly by default.
  return kind === "watch" ? null : "weekly";
}
