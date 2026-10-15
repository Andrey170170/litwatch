/** Acknowledgement toasts: a tiny store read by `<Toaster>`. Only what the page cannot show lands here. */
import { useSyncExternalStore } from "react";

export type Toast = { id: number; text: string; tone: "plain" | "error" };

let toasts: Toast[] = [];
let next = 1;
const listeners = new Set<() => void>();

function emit() {
  for (const l of listeners) l();
}

export function toast(text: string | null | undefined, tone: Toast["tone"] = "plain", ms = 2600) {
  if (!text) return;
  const id = next++;
  toasts = [...toasts, { id, text, tone }];
  emit();
  window.setTimeout(() => {
    toasts = toasts.filter((t) => t.id !== id);
    emit();
  }, ms);
}

/** Drop every toast at once; tests reset the store between cases. */
export function clearToasts() {
  toasts = [];
  emit();
}

export function useToasts(): Toast[] {
  return useSyncExternalStore(
    (cb) => {
      listeners.add(cb);
      return () => listeners.delete(cb);
    },
    () => toasts,
    () => toasts,
  );
}
