/**
 * One policy for every write that can move a grade, a reaction or the library: everything
 * that shows a paper, a count or the status line is refreshed.
 */
import type { QueryClient } from "@tanstack/react-query";

export function invalidateAfterWrite(qc: QueryClient) {
  for (const key of ["paper", "issue", "digest", "papers", "topics", "lookup", "status", "settings"]) {
    void qc.invalidateQueries({ queryKey: [key] });
  }
}
