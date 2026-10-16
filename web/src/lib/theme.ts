/**
 * The appearance, kept on the device: System follows the OS; Light and Dark are forced through `<html data-theme>`,
 * which the stylesheet keys on and which index.html sets from the same key before first paint (DESIGN.md,
 * "Settings"). The PWA's bar colour follows the forced side too.
 */
import { useSyncExternalStore } from "react";

export const APPEARANCES = ["system", "light", "dark"] as const;
export type Appearance = (typeof APPEARANCES)[number];
export const KEY = "litwatch.appearance";
const BAR = { light: "#e9ebe7", dark: "#07090d" };

const watchers = new Set<() => void>();
const watch = (w: () => void) => {
  watchers.add(w);
  return () => void watchers.delete(w);
};

/** The choice kept on the device; System for none, or anything but a side. */
export function readAppearance(): Appearance {
  try {
    const v = localStorage.getItem(KEY);
    return v === "light" || v === "dark" ? v : "system";
  } catch {
    return "system";
  }
}

/** Put the appearance on the document: the forced side, or none to follow the OS. */
export function applyAppearance(a: Appearance) {
  const html = document.documentElement;
  if (a === "system") delete html.dataset.theme;
  else html.dataset.theme = a;
  for (const m of document.querySelectorAll<HTMLMetaElement>('meta[name="theme-color"][media]')) {
    m.content = BAR[a === "system" ? (m.getAttribute("media")?.includes("dark") ? "dark" : "light") : a];
  }
}

export function setAppearance(a: Appearance) {
  try {
    if (a === "system") localStorage.removeItem(KEY);
    else localStorage.setItem(KEY, a);
  } catch {
    // a device that keeps nothing: the choice holds for the page
  }
  applyAppearance(a);
  watchers.forEach((w) => w());
}

export const useAppearance = () => useSyncExternalStore(watch, readAppearance, () => "system" as Appearance);
