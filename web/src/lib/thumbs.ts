/**
 * The thumbs' state machine (DESIGN.md "Thumbs"), separate from the component so it can be
 * tested on its own. A thumb up: tap 1 sets it, tap 2 offers Great find, tap 3 clears it. A
 * thumb down: tap 1 sets it and opens the reasons, tap 2 clears it. Picking a reason or Great
 * find records it and closes the chips.
 */
import type { ReactionReason, ReactionValue } from "@/api/client";

export type ThumbState = {
  value: ReactionValue | null;
  reason: ReactionReason | null;
  /** Which chips are open: the down reasons, the Great find offer, or none. */
  open: "reasons" | "great" | null;
};

export type ThumbAction =
  | { type: "tapUp" }
  | { type: "tapDown" }
  | { type: "pick"; reason: ReactionReason }
  | { type: "dismiss" };

export type ThumbEffect =
  | { kind: "put"; value: ReactionValue; reason: ReactionReason | null }
  | { kind: "delete" }
  | { kind: "none" };

export const DOWN_REASONS: { reason: ReactionReason; label: string }[] = [
  { reason: "not_for_me", label: "Not for me" },
  { reason: "unrelated", label: "Unrelated to the topic" },
  { reason: "already_knew", label: "Already knew it" },
  { reason: "overrated", label: "Overrated" },
];

export function initial(value: ReactionValue | null | undefined, reason: ReactionReason | null | undefined): ThumbState {
  return { value: value ?? null, reason: reason ?? null, open: null };
}

export function reduce(state: ThumbState, action: ThumbAction): [ThumbState, ThumbEffect] {
  switch (action.type) {
    case "tapUp":
      if (state.value !== "up") return [{ value: "up", reason: null, open: null }, { kind: "put", value: "up", reason: null }];
      if (state.open !== "great" && state.reason !== "great_find") return [{ ...state, open: "great" }, { kind: "none" }];
      return [{ value: null, reason: null, open: null }, { kind: "delete" }];
    case "tapDown":
      if (state.value !== "down")
        return [{ value: "down", reason: null, open: "reasons" }, { kind: "put", value: "down", reason: null }];
      return [{ value: null, reason: null, open: null }, { kind: "delete" }];
    case "pick": {
      const value: ReactionValue = action.reason === "great_find" ? "up" : "down";
      return [{ value, reason: action.reason, open: null }, { kind: "put", value, reason: action.reason }];
    }
    case "dismiss":
      return [{ ...state, open: null }, { kind: "none" }];
  }
}
