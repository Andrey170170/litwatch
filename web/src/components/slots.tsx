/**
 * Slots the phone shell leaves for the page: its header title or bar, and its bottom bar. A page fills them with
 * `<Fill>`, which portals into the slot's element, so the page keeps its own state and the shell its layout.
 */
import { useLayoutEffect, useRef, useSyncExternalStore, type ReactNode } from "react";
import { createPortal } from "react-dom";

type Name = "title" | "bar" | "foot";
const elements: Partial<Record<Name, HTMLElement | null>> = {};
const listeners = new Set<() => void>();
const emit = () => listeners.forEach((l) => l());
const subscribe = (cb: () => void) => {
  listeners.add(cb);
  return () => listeners.delete(cb);
};

export function Slot({ name, className }: { name: Name; className?: string }) {
  const ref = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => {
    elements[name] = ref.current;
    emit();
    return () => {
      elements[name] = null;
      emit();
    };
  }, [name]);
  return <div ref={ref} className={className} data-slot={name} />;
}

export function Fill({ name, children }: { name: Name; children: ReactNode }) {
  const el = useSyncExternalStore(subscribe, () => elements[name] ?? null, () => null);
  return el ? createPortal(children, el) : null;
}
