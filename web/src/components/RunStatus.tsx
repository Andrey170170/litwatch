import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { summaryQuery } from "@/api/queries";
import { statusLine } from "@/lib/status";

/** The run status in the wordmark; it opens the Settings page that explains it. Read from the shell's summary, by
 * its status alone: nothing else changing in it re-renders the line. */
export function RunStatus() {
  const { data } = useQuery({ ...summaryQuery, select: (s) => s.status });
  const line = data ? statusLine(data) : null;
  if (!line) return null;
  return (
    <Link to="/settings/$page" params={{ page: line.page }} className={`st ${line.tone}`} title={line.title}>
      {line.text}
    </Link>
  );
}
