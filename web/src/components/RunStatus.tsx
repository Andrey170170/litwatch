import { Link } from "@tanstack/react-router";
import { statusLine } from "@/lib/status";
import { Unavailable, useSummary } from "./Summary";

/** The run status in the wordmark; it opens the Settings page that explains it. Read from the shell's summary, by
 * its status alone: nothing else changing in it re-renders the line. A summary never read says so in its place. */
export function RunStatus() {
  const { data, unavailable, retry } = useSummary((s) => s.status);
  if (unavailable) return <Unavailable what="status" retry={retry} line />;
  const line = data ? statusLine(data) : null;
  if (!line) return null;
  return (
    <Link to="/settings/$page" params={{ page: line.page }} className={`st ${line.tone}`} title={line.title}>
      {line.text}
    </Link>
  );
}
