import { useSyncExternalStore } from "react";

const QUERY = "(min-width: 56rem)";

function subscribe(cb: () => void) {
  const mq = window.matchMedia(QUERY);
  mq.addEventListener("change", cb);
  return () => mq.removeEventListener("change", cb);
}

/** True at the desktop layout (the `desk:` breakpoint in styles.css); the phone below it. */
export function useDesktop(): boolean {
  return useSyncExternalStore(subscribe, () => window.matchMedia(QUERY).matches, () => true);
}
