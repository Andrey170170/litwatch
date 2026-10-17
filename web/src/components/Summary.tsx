/**
 * The shell's summary as its parts read it: one field each, so nothing else changing in it re-renders them, and
 * one word for the summary failing before anything landed. TanStack keeps what a read loaded over a failed
 * refresh, so only the first read failing leaves the shell with nothing; `Unavailable` names that in the part's
 * place, with the way to try again, instead of leaving the status, the topics or the counts blank.
 */
import { useQuery } from "@tanstack/react-query";
import type { Schemas } from "@/api/client";
import { summaryQuery } from "@/api/queries";

export function useSummary<T>(select: (s: Schemas["Summary"]) => T): { data: T | undefined; unavailable: boolean; retry: () => void } {
  const q = useQuery({ ...summaryQuery, select, notifyOnChangeProps: ["data", "status"] });
  return { data: q.data, unavailable: q.isError && q.data === undefined, retry: () => void q.refetch() };
}

/** "status unavailable · Try again": as the status line (`line`) or as a line in a sidebar's list. */
export function Unavailable({ what, retry, line }: { what: string; retry: () => void; line?: boolean }) {
  return (
    <span className={line ? "st failed" : "loadmore"}>
      {what} unavailable ·{" "}
      <button className="inl" onClick={retry}>
        Try again
      </button>
    </span>
  );
}
