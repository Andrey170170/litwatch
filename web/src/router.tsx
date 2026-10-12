import { createRootRoute, createRoute, createRouter, type RouterHistory } from "@tanstack/react-router";
import { Shell } from "@/components/Shell";
import { DigestPage } from "@/routes/digest";
import { PaperPage } from "@/routes/paper";
import { PapersPage } from "@/routes/papers";
import { SettingsIndex, SettingsLayout } from "@/routes/settings/index";
import { TopicEditorPage, TopicsSettings } from "@/routes/settings/topics";
import { QualityPage } from "@/routes/settings/quality";
import { CalibratePage } from "@/routes/settings/calibrate";
import { ConfigPage, IntegrationsPage, MissesPage, RunsPage } from "@/routes/settings/plain";
import { TopicPage, TopicsIndex, TopicsLayout } from "@/routes/topics";
import { bool, int, str, tableSearch, type TableSearch } from "@/lib/search";

const rootRoute = createRootRoute({ component: Shell });

/** An arXiv link or bare id inside shared text, else the text itself for the lookup to judge. */
export function arxivIn(text: string | undefined): string | undefined {
  if (!text) return undefined;
  const link = text.match(/https?:\/\/(?:www\.)?arxiv\.org\/(?:abs|pdf|html)\/[\w.\/-]+/i);
  if (link) return link[0];
  const id = text.match(/\b(?:arXiv:)?(\d{4}\.\d{4,5}(?:v\d+)?)\b/i);
  return id ? id[1] : text;
}

export type DigestSearch = { issue?: number; paper?: string; topic?: string };
export type PaperSearch = { topic?: string };
export type PapersSearch = TableSearch & { topic?: string; lookup?: string };

export const digestRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/",
  validateSearch: (s: Record<string, unknown>): DigestSearch => ({ issue: int(s.issue), paper: str(s.paper), topic: str(s.topic) }),
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
    // The share target: the explicit url first, else an arXiv link or id found in the text.
    lookup: str(s.lookup) ?? str(s.url) ?? arxivIn(str(s.text)),
    adjacent: bool(s.adjacent),
  }),
  component: PapersPage,
});

export const settingsRoute = createRoute({ getParentRoute: () => rootRoute, path: "/settings", component: SettingsLayout });
export const settingsIndexRoute = createRoute({ getParentRoute: () => settingsRoute, path: "/", component: SettingsIndex });
export const settingsTopicsRoute = createRoute({ getParentRoute: () => settingsRoute, path: "/topics", component: TopicsSettings });
export const settingsTopicRoute = createRoute({ getParentRoute: () => settingsRoute, path: "/topics/$slug", component: TopicEditorPage });
export const settingsQualityRoute = createRoute({ getParentRoute: () => settingsRoute, path: "/quality", component: QualityPage });
export const settingsCalibrateRoute = createRoute({
  getParentRoute: () => settingsRoute,
  path: "/calibrate/$id",
  validateSearch: (s: Record<string, unknown>): { at?: number } => ({ at: int(s.at) }),
  component: CalibratePage,
});
export const settingsMissesRoute = createRoute({ getParentRoute: () => settingsRoute, path: "/misses", component: MissesPage });
export const settingsRunsRoute = createRoute({ getParentRoute: () => settingsRoute, path: "/runs", component: RunsPage });
export const settingsIntegrationsRoute = createRoute({ getParentRoute: () => settingsRoute, path: "/integrations", component: IntegrationsPage });
export const settingsConfigRoute = createRoute({ getParentRoute: () => settingsRoute, path: "/configuration", component: ConfigPage });

const routeTree = rootRoute.addChildren([
  digestRoute,
  paperRoute,
  topicsRoute.addChildren([topicsIndexRoute, topicRoute]),
  papersRoute,
  settingsRoute.addChildren([
    settingsIndexRoute,
    settingsTopicsRoute,
    settingsTopicRoute,
    settingsQualityRoute,
    settingsCalibrateRoute,
    settingsMissesRoute,
    settingsRunsRoute,
    settingsIntegrationsRoute,
    settingsConfigRoute,
  ]),
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

export function createAppRouter(history?: RouterHistory) {
  return createRouter({
    routeTree,
    history,
    scrollRestoration: true,
    defaultPreload: "intent",
    parseSearch,
    stringifySearch,
  });
}

export const router = createAppRouter();

declare module "@tanstack/react-router" {
  interface Register {
    router: typeof router;
  }
}
