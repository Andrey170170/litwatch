/**
 * Where the Android share target lands (DESIGN.md, "Phone"; the manifest's `share_target`): the link, text and
 * title shared are read for an arXiv id, and the paper opens with "Report as a miss" ready; without an id, Papers
 * searches what was shared. Replaced in the history, so Back leaves the app as a share should.
 */
import { Navigate } from "@tanstack/react-router";
import { landingFor } from "@/lib/share";
import { shareRoute } from "@/router";

export function SharePage() {
  const landing = landingFor(shareRoute.useSearch());
  if (landing.kind === "paper") return <Navigate to="/paper/$" params={{ _splat: landing.id }} search={{ miss: true }} replace />;
  return <Navigate to="/papers" search={landing.q ? { q: landing.q } : {}} replace />;
}
