import { createRootRoute, createRoute, createRouter } from "@tanstack/react-router";
import { Shell } from "@/components/Shell";
import { DigestPage } from "@/routes/digest";
import { PaperPage } from "@/routes/paper";
import { PapersPage } from "@/routes/papers";
import { SettingsIndex, SettingsPage, SettingsPlaceholder } from "@/routes/settings";
import { TopicPage, TopicsIndex, TopicsLayout } from "@/routes/topics";
import { bool, int, str, tableSearch, type TableSearch } from "@/lib/search";

const rootRoute = createRootRoute({ component: Shell });

export type DigestSearch = { issue?: number; paper?: string };
export type PaperSearch = { topic?: string };
export type PapersSearch = TableSearch & { topic?: string; lookup?: string };

export const digestRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/",
  validateSearch: (s: Record<string, unknown>): DigestSearch => ({ issue: int(s.issue), paper: str(s.paper) }),
  component: DigestPage,
});

// The id is a splat: old arXiv ids carry a slash (`cs/9901001`).
export const paperRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/paper/$",
  validateSearch: (s: Record<string, unknown>): PaperSearch => ({ topic: str(s.topic) }),
  component: PaperPage,
});

export const topicsRoute = createRoute({ getParentRoute: () => rootRoute, path: "/topics", component: TopicsLayout });
export const topicsIndexRoute = createRoute({ getParentRoute: () => topicsRoute, path: "/", component: TopicsIndex });
export const topicRoute = createRoute({
  getParentRoute: () => topicsRoute,
  path: "/$slug",
  validateSearch: tableSearch,
  component: TopicPage,
});

export const papersRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/papers",
  validateSearch: (s: Record<string, unknown>): PapersSearch => ({
    ...tableSearch(s),
    topic: str(s.topic),
    lookup: str(s.lookup) ?? str(s.text) ?? str(s.url),
    adjacent: bool(s.adjacent),
  }),
  component: PapersPage,
});

export const settingsRoute = createRoute({ getParentRoute: () => rootRoute, path: "/settings", component: SettingsPage });
export const settingsIndexRoute = createRoute({ getParentRoute: () => settingsRoute, path: "/", component: SettingsIndex });
export const settingsPageRoute = createRoute({ getParentRoute: () => settingsRoute, path: "/$page", component: SettingsPlaceholder });

const routeTree = rootRoute.addChildren([
  digestRoute,
  paperRoute,
  topicsRoute.addChildren([topicsIndexRoute, topicRoute]),
  papersRoute,
  settingsRoute.addChildren([settingsIndexRoute, settingsPageRoute]),
]);

// Plain query strings (`?paper=2505.17244`, not JSON-quoted), so share links stay readable;
// each route's validateSearch turns the strings back into numbers and booleans.
function parseSearch(search: string): Record<string, string> {
  return Object.fromEntries(new URLSearchParams(search));
}
function stringifySearch(search: Record<string, unknown>): string {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(search)) if (v !== undefined && v !== null && v !== "") p.set(k, String(v));
  const s = p.toString();
  return s ? `?${s}` : "";
}

export const router = createRouter({
  routeTree,
  scrollRestoration: true,
  defaultPreload: "intent",
  parseSearch,
  stringifySearch,
});

declare module "@tanstack/react-router" {
  interface Register {
    router: typeof router;
  }
}
