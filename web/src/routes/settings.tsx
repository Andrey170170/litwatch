/**
 * Settings, behind the gear (DESIGN.md, "Settings"): the desktop's sidebar with the pages in three groups, the
 * phone's list of them with a line under each, and the page itself by its name.
 */
import { useQuery } from "@tanstack/react-query";
import { Link, Navigate, useParams } from "@tanstack/react-router";
import { memo } from "react";
import { summaryQuery } from "@/api/queries";
import { SideHead } from "@/components/Shell";
import { Fill } from "@/components/slots";
import { useDesktop } from "@/hooks/useDesktop";
import { rowDate } from "@/lib/format";
import { countRender } from "@/lib/renders";
import { GROUPS, isPage, type Page, usd } from "@/lib/settings";
import { CalibratePage } from "./settings/calibrate";
import { ConfigurationPage } from "./settings/configuration";
import { IntegrationsPage } from "./settings/integrations";
import { MissesPage } from "./settings/misses";
import { NeedsPage } from "./settings/needs";
import { OverviewPage } from "./settings/overview";
import { RunsPage } from "./settings/runs";
import { TaxonomyPage } from "./settings/taxonomy";
import { TopicsPage } from "./settings/topics";

/** What each page's hint says, and the count beside it in the sidebar (Needs you asks, yellow; a failed integration,
 * red), from the shell's summary: the pages' own reads never feed the sidebar. Empty before the first read. */
function useHints(): Record<Page, { hint: string; count?: number; tone?: "ask" | "hot" }> {
  const { data: h } = useQuery({ ...summaryQuery, select: (s) => s.settings });
  const failing = h?.failing ?? [];
  if (!h) return Object.fromEntries(GROUPS.flatMap((g) => g.pages).map((p) => [p.id, { hint: "" }])) as Record<Page, { hint: string }>;
  return {
    overview: { hint: "Recall, precision, reactions" },
    needs: { hint: h.waiting ? `${h.waiting} waiting` : "All settled", count: h.waiting || undefined, tone: "ask" },
    calibrate: { hint: h.last_session ? `Last session ${rowDate(h.last_session)}` : "No session yet" },
    misses: { hint: h.misses ? `${h.misses} reported` : "None reported" },
    topics: { hint: `${h.topics} followed` },
    taxonomy: { hint: `${h.classes} classes` },
    runs: { hint: h.budget ? `${usd(h.spent)} of ${usd(h.budget)} this month` : `${usd(h.spent)} this month` },
    integrations: { hint: failing.length ? `${failing.join(", ")}: failed` : "Up to date", count: failing.length || undefined, tone: "hot" },
    configuration: { hint: "litwatch.toml, read-only" },
  };
}

/** The desktop sidebar while Settings is open: a way back to reading, then the index with its counts. Memoised:
 * the page changing under it re-renders only its active links. */
export const SettingsSide = memo(function SettingsSide() {
  const hints = useHints();
  countRender("settings-side");
  return (
    <aside className="side">
      <SideHead />
      <nav className="nav">
        <Link to="/" className="it leave">
          ‹ Back to reading
        </Link>
        <div className="sec setttl">⚙ Settings</div>
        {GROUPS.map((g) => (
          <div key={g.name}>
            <div className="sec">{g.name}</div>
            {g.pages.map((p) => (
              <Link key={p.id} to="/settings/$page" params={{ page: p.id }} className="it" activeProps={{ className: "it on" }} activeOptions={{ exact: false }}>
                <span className="nm">{p.name}</span>
                {hints[p.id].count ? <span className={`k ${hints[p.id].tone}`}>{hints[p.id].count}</span> : null}
              </Link>
            ))}
          </div>
        ))}
      </nav>
    </aside>
  );
});

/** The gear's landing: the phone's list of pages; the desktop goes straight to Overview. */
export function SettingsIndex() {
  const desktop = useDesktop();
  const hints = useHints();
  if (desktop) return <Navigate to="/settings/$page" params={{ page: "overview" }} replace />;
  return (
    <>
      <Fill name="title">
        <span className="t">Settings</span>
      </Fill>
      <div className="setlist">
        {GROUPS.map((g) => (
          <div key={g.name}>
            <div className="sec">{g.name}</div>
            {g.pages.map((p) => (
              <Link key={p.id} to="/settings/$page" params={{ page: p.id }} className="it" data-page={p.id}>
                <span className="nm">{p.name}</span>
                <span className={`hint${hints[p.id].count ? ` ${hints[p.id].tone}` : ""}`}>{hints[p.id].hint}</span>
              </Link>
            ))}
          </div>
        ))}
      </div>
    </>
  );
}

const PAGES: Record<Page, () => React.JSX.Element> = {
  overview: OverviewPage,
  needs: NeedsPage,
  calibrate: CalibratePage,
  misses: MissesPage,
  topics: TopicsPage,
  taxonomy: TaxonomyPage,
  runs: RunsPage,
  integrations: IntegrationsPage,
  configuration: ConfigurationPage,
};

export function SettingsPage() {
  const { page } = useParams({ from: "/settings/$page" });
  if (!isPage(page)) return <p className="empty">There is no such page.</p>;
  const Component = PAGES[page];
  return (
    <div className="setpg">
      <Component />
    </div>
  );
}
