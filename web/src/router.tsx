import { createRootRoute, createRoute, createRouter, type RouterHistory } from "@tanstack/react-router";
import { Shell } from "@/components/Shell";
import { isIsoDay } from "@/lib/digests";
import { listSearch, parseSearch, str, stringifySearch } from "@/lib/search";
import { DigestOnePage, DigestPage, HistoryPage, WeekPage } from "@/routes/digest";
import { PaperPage } from "@/routes/paper";
import { PapersPage } from "@/routes/papers";
import { SettingsIndex, SettingsPage } from "@/routes/settings";
import { SessionPage } from "@/routes/settings/calibrate";
import { EventsPage } from "@/routes/settings/integrations";
import { JobsPage } from "@/routes/settings/runs";
import { TopicEditorPage } from "@/routes/settings/topics";
import { TopicPage } from "@/routes/topic";
import { TopicsPage } from "@/routes/topics";

declare module "@tanstack/react-router" {
  interface HistoryState {
    /** The key of the list a paper was opened from (listContext.ts). */
    list?: string;
    /** The paper was opened from Settings, so it keeps the Settings sidebar. */
    settings?: boolean;
  }
}

/** A path no route owns stays inside the shell, so the sidebar is the way out. */
function NotFound() {
  return <p className="empty">There is no such page.</p>;
}

const rootRoute = createRootRoute({ component: Shell, notFoundComponent: NotFound });

// The Digest's views: Day (`day`, today without), Week (`week`, a day of the week shown, last week's without) and
// History (`view` cal, the list without). `digest` on any of them is the digest in the desktop's slide-over.
const digestOf = (s: Record<string, unknown>) => {
  const id = Number(s.digest);
  return Number.isInteger(id) && id > 0 ? { digest: id } : {};
};
export const digestRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/",
  validateSearch: (s: Record<string, unknown>): { day?: string; digest?: number } => ({ ...(isIsoDay(s.day) ? { day: s.day } : {}), ...digestOf(s) }),
  component: DigestPage,
});
export const weekRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/week",
  validateSearch: (s: Record<string, unknown>): { week?: string; digest?: number } => ({ ...(isIsoDay(s.week) ? { week: s.week } : {}), ...digestOf(s) }),
  component: WeekPage,
});
export const historyRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/history",
  validateSearch: (s: Record<string, unknown>): { view?: "cal"; digest?: number } => ({ ...(s.view === "cal" ? { view: "cal" as const } : {}), ...digestOf(s) }),
  component: HistoryPage,
});
function DigestById() {
  const { id } = digestOneRoute.useParams();
  return <DigestOnePage id={Number(id)} />;
}
export const digestOneRoute = createRoute({ getParentRoute: () => rootRoute, path: "/digests/$id", component: DigestById });
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
// Settings: the index, a page, and the pages inside pages (a topic's editor, a Calibrate session, Jobs, events).
export const settingsRoute = createRoute({ getParentRoute: () => rootRoute, path: "/settings", component: SettingsIndex });
export const settingsPageRoute = createRoute({ getParentRoute: () => rootRoute, path: "/settings/$page", component: SettingsPage });
export const settingsTopicRoute = createRoute({ getParentRoute: () => rootRoute, path: "/settings/topics/$slug", component: TopicEditorPage });
export const settingsSessionRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/settings/calibrate/$id",
  // `at` is the paper on screen, 1-based; without it the session opens at its first unanswered paper, or its review
  // once every paper is answered; `review` opens the review before that.
  validateSearch: (s: Record<string, unknown>): { at?: number; review?: boolean } => {
    const at = Number(s.at);
    return { ...(Number.isInteger(at) && at > 0 ? { at } : {}), ...(s.review === "1" || s.review === true ? { review: true } : {}) };
  },
  component: SessionPage,
});
export const settingsJobsRoute = createRoute({ getParentRoute: () => rootRoute, path: "/settings/runs/jobs", component: JobsPage });
export const settingsEventsRoute = createRoute({ getParentRoute: () => rootRoute, path: "/settings/integrations/$name", component: EventsPage });

const routeTree = rootRoute.addChildren([digestRoute, weekRoute, historyRoute, digestOneRoute, topicsRoute, topicRoute, papersRoute, paperRoute, settingsRoute, settingsPageRoute, settingsTopicRoute, settingsSessionRoute, settingsJobsRoute, settingsEventsRoute]);

export function createAppRouter(history?: RouterHistory) {
  return createRouter({ routeTree, history, scrollRestoration: true, scrollToTopSelectors: [".main", ".screen"], parseSearch, stringifySearch });
}

export const router = createAppRouter();

declare module "@tanstack/react-router" {
  interface Register {
    router: typeof router;
  }
}
