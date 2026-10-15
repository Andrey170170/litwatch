import { createRootRoute, createRoute, createRouter, type RouterHistory } from "@tanstack/react-router";
import { Shell } from "@/components/Shell";
import { listSearch, parseSearch, str, stringifySearch } from "@/lib/search";
import { DigestPage } from "@/routes/digest";
import { PaperPage } from "@/routes/paper";
import { PapersPage } from "@/routes/papers";
import { SettingsPage } from "@/routes/settings";
import { TopicPage } from "@/routes/topic";
import { TopicsPage } from "@/routes/topics";

declare module "@tanstack/react-router" {
  interface HistoryState {
    /** The key of the list a paper was opened from (listContext.ts). */
    list?: string;
  }
}

/** A path no route owns stays inside the shell, so the sidebar is the way out. */
function NotFound() {
  return <p className="empty">There is no such page.</p>;
}

const rootRoute = createRootRoute({ component: Shell, notFoundComponent: NotFound });

export const digestRoute = createRoute({ getParentRoute: () => rootRoute, path: "/", component: DigestPage });
export const topicsRoute = createRoute({ getParentRoute: () => rootRoute, path: "/topics", component: TopicsPage });
export const topicRoute = createRoute({ getParentRoute: () => rootRoute, path: "/topics/$slug", validateSearch: listSearch, component: TopicPage });
export const papersRoute = createRoute({ getParentRoute: () => rootRoute, path: "/papers", validateSearch: listSearch, component: PapersPage });
// The id is a splat: old arXiv ids carry a slash (`cs/9901001`). `topic` is the topic the paper was opened under.
export const paperRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/paper/$",
  validateSearch: (s: Record<string, unknown>): { topic?: string } => {
    const topic = str(s.topic);
    return topic ? { topic } : {};
  },
  component: PaperPage,
});
export const settingsRoute = createRoute({ getParentRoute: () => rootRoute, path: "/settings/$page", component: SettingsPage });

const routeTree = rootRoute.addChildren([digestRoute, topicsRoute, topicRoute, papersRoute, paperRoute, settingsRoute]);

export function createAppRouter(history?: RouterHistory) {
  return createRouter({ routeTree, history, scrollRestoration: true, scrollToTopSelectors: [".main", ".screen"], parseSearch, stringifySearch });
}

export const router = createAppRouter();

declare module "@tanstack/react-router" {
  interface Register {
    router: typeof router;
  }
}
