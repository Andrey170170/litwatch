/**
 * Settings, behind the gear (DESIGN.md, "Settings"): the desktop's sidebar with the pages in three groups, the
 * phone's list of them with a line under each, and the page itself by its name.
 */
import { Link, Navigate, useParams } from "@tanstack/react-router";
import { memo } from "react";
import type { Schemas } from "@/api/client";
import { SideHead } from "@/components/Shell";
import { Unavailable, useSummary } from "@/components/Summary";
import { Fill } from "@/components/slots";
import { useDesktop } from "@/hooks/useDesktop";
import { rowDate } from "@/lib/format";
import { countRender } from "@/lib/renders";
import { GROUPS, isPage, type Page, usd } from "@/lib/settings";
import { APPEARANCES, setAppearance, useAppearance, type Appearance } from "@/lib/theme";
import { CalibratePage } from "./settings/calibrate";
import { ConfigurationPage } from "./settings/configuration";
import { IntegrationsPage } from "./settings/integrations";
import { MissesPage } from "./settings/misses";
import { NeedsPage } from "./settings/needs";
import { OverviewPage } from "./settings/overview";
import { RunsPage } from "./settings/runs";
import { TaxonomyPage } from "./settings/taxonomy";
import { TopicsPage } from "./settings/topics";

type Hints = Record<Page, { hint: string; count?: number; tone?: "ask" | "hot" }>;

/** What each page's hint says, and the count beside it in the sidebar (Needs you asks, yellow; a failed integration,
 * red), from the shell's summary: the pages' own reads never feed the sidebar. Empty before the first read, and
 * `unavailable` when that read failed, for the index to say so. */
function useHints(): { hints: Hints; unavailable: boolean; retry: () => void } {
  const { data: h, unavailable, retry } = useSummary((s) => s.settings);
  const failing = h?.failing ?? [];
  const empty = () => Object.fromEntries(GROUPS.flatMap((g) => g.pages).map((p) => [p.id, { hint: "" }])) as Hints;
  return { hints: h ? hintsOf(h, failing) : empty(), unavailable, retry };
}

function hintsOf(h: Schemas["SettingsHints"], failing: string[]): Hints {
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

const APPEARANCE_NAMES: Record<Appearance, string> = { system: "System", light: "Light", dark: "Dark" };

/** The appearance, kept on the device: System follows the OS (DESIGN.md, "Settings"). */
function AppearanceControl() {
  const current = useAppearance();
  return (
    <div className="appr" role="group" aria-label="Appearance">
      {APPEARANCES.map((a) => (
        <button key={a} type="button" className={current === a ? "on" : undefined} aria-pressed={current === a} onClick={() => setAppearance(a)}>
          {APPEARANCE_NAMES[a]}
        </button>
      ))}
    </div>
  );
}

/** The desktop sidebar while Settings is open: a way back to reading, then the index with its counts. Memoised:
 * the page changing under it re-renders only its active links. */
export const SettingsSide = memo(function SettingsSide() {
  const { hints, unavailable, retry } = useHints();
  countRender("settings-side");
  return (
    <aside className="side">
      <SideHead />
      <nav className="nav">
        <Link to="/" className="it leave">
          ‹ Back to reading
        </Link>
        <div className="sec setttl">⚙ Settings</div>
        {unavailable && <Unavailable what="counts" retry={retry} />}
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
      <div className="foot">
        <div className="sec">Appearance</div>
        <AppearanceControl />
      </div>
    </aside>
  );
});

/** The gear's landing: the phone's list of pages; the desktop goes straight to Overview. */
export function SettingsIndex() {
  const desktop = useDesktop();
  const { hints, unavailable, retry } = useHints();
  if (desktop) return <Navigate to="/settings/$page" params={{ page: "overview" }} replace />;
  return (
    <>
      <Fill name="title">
        <span className="t">Settings</span>
      </Fill>
      <div className="setlist">
        {unavailable && <Unavailable what="counts" retry={retry} />}
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
        <div>
          <div className="sec">Appearance</div>
          <AppearanceControl />
        </div>
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
