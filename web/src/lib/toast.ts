/** Acknowledgement toasts: a tiny store, read by `<Toaster>`. Every write's `message` lands here. */
import { useSyncExternalStore } from "react";

export type Toast = { id: number; text: string; tone: "plain" | "error" };

let toasts: Toast[] = [];
let next = 1;
const listeners = new Set<() => void>();

function emit() {
  for (const l of listeners) l();
}

export function toast(text: string, tone: Toast["tone"] = "plain", ms = 3500) {
  const id = next++;
  toasts = [...toasts, { id, text, tone }];
  emit();
  window.setTimeout(() => {
    toasts = toasts.filter((t) => t.id !== id);
    emit();
  }, ms);
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
