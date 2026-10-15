/** A render counter for the shell's tests, which assert the sidebars never re-render as pages change: counts into
 * `globalThis.__renders` when a test has set it, a no-op otherwise. */
export function countRender(name: string): void {
  const counts = (globalThis as { __renders?: Record<string, number> }).__renders;
  if (counts) counts[name] = (counts[name] ?? 0) + 1;
}
