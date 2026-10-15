import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { statusQuery } from "@/api/queries";
import { statusLine } from "@/lib/status";

/** The run status in the wordmark; it opens the Settings page that explains it. */
export function RunStatus() {
  const { data } = useQuery(statusQuery);
  const line = data ? statusLine(data) : null;
  if (!line) return null;
  return (
    <Link to="/settings/$page" params={{ page: line.page }} className={`st ${line.tone}`} title={line.title}>
      {line.text}
    </Link>
  );
}
