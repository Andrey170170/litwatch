/**
 * The shell (DESIGN.md, "Shell"): on the desktop a sidebar and the main column; on the phone a header, the page,
 * the page's bottom bar, and the drawer. The main column records how it was reached (go, back, top) so the page
 * can slide in from the right side.
 */
import { useQuery } from "@tanstack/react-query";
import { Link, Outlet, useLocation, useRouter } from "@tanstack/react-router";
import { useEffect, useRef, useState } from "react";
import { topicsQuery } from "@/api/queries";
import { useDesktop } from "@/hooks/useDesktop";
import { Badge } from "./Badge";
import { RunStatus } from "./RunStatus";
import { Sheet } from "./Sheet";
import { Slot } from "./slots";
import { Toaster } from "./Toaster";

const TABS = [
  { to: "/", name: "Digest" },
  { to: "/topics", name: "Topics" },
  { to: "/papers", name: "Papers" },
] as const;

/** A deeper screen has "‹ Back" (desktop) or "‹" and its title (phone); a top-level one the tabs' chrome. */
export function isDeeper(pathname: string): boolean {
  return !["/", "/topics", "/papers"].includes(pathname.replace(/\/$/, "") || "/");
}

type Came = "go" | "back" | "top" | "keep";

/** How the current page was reached, from the history index: forward is deeper, backward is back. */
function useCame(): Came {
  const location = useLocation();
  const last = useRef<{ key: string; index: number } | null>(null);
  const index = (location.state as { __TSR_index?: number }).__TSR_index ?? 0;
  const key = location.pathname;
  if (last.current?.key === key) return "keep";
  const before = last.current;
  last.current = { key, index };
  if (!before || !isDeeper(key)) return "top";
  return index < before.index ? "back" : "go";
}

function Following({ onPick }: { onPick?: () => void }) {
  const { data } = useQuery(topicsQuery);
  return (
    <>
      <div className="sec">Following</div>
      {data?.flatMap((g) => g.topics).map((t) => (
        <Link key={t.slug} to="/topics/$slug" params={{ slug: t.slug }} className="it" activeProps={{ className: "it on" }} onClick={onPick}>
          <Badge topic={t} />
          <span className="nm">{t.name}</span>
        </Link>
      ))}
    </>
  );
}

function Tabs({ onPick, drawer }: { onPick?: () => void; drawer?: boolean }) {
  return TABS.map((t) => (
    <Link key={t.to} to={t.to} className="it tab" activeOptions={{ exact: true }} activeProps={{ className: "it tab on" }} onClick={onPick}>
      {t.name}
      {drawer && t.name === "Digest" ? null : null}
    </Link>
  ));
}

function Desktop() {
  const came = useCame();
  return (
    <div className="app">
      <aside className="side">
        <div className="wm">
          <Link to="/">litwatch</Link>
          <RunStatus />
        </div>
        <nav className="nav">
          <Tabs />
          <Following />
        </nav>
        <nav className="foot nav">
          <Link to="/settings/$page" params={{ page: "overview" }} className="it" activeProps={{ className: "it on" }}>
            ⚙ Settings
          </Link>
        </nav>
      </aside>
      <main className="main" data-came={came}>
        <div className="inner">
          <Outlet />
        </div>
      </main>
      <Toaster />
    </div>
  );
}

function Phone() {
  const location = useLocation();
  const router = useRouter();
  const came = useCame();
  const deeper = isDeeper(location.pathname);
  const [menu, setMenu] = useState(false);
  useEffect(() => setMenu(false), [location.pathname]);
  const back = () => (router.history.canGoBack() ? router.history.back() : router.navigate({ to: "/" }));
  return (
    <div className="phone">
      <header className="pbar">
        {deeper ? (
          <>
            <button className="ib" onClick={back} aria-label="Back">
              ‹
            </button>
            <Slot name="title" className="ttl" />
          </>
        ) : (
          <>
            <button className="ib" onClick={() => setMenu(true)} aria-label="Menu">
              ☰
            </button>
            <Slot name="bar" className="ttl" />
          </>
        )}
        <span className="sp" />
        <RunStatus />
        <Link to="/settings/$page" params={{ page: "overview" }} className="ib" aria-label="Settings">
          ⚙
        </Link>
      </header>
      <div className="scroll screen" data-came={came} key={location.pathname}>
        <div className="pp">
          <Outlet />
        </div>
      </div>
      <Slot name="foot" />
      <Sheet open={menu} onClose={() => setMenu(false)} side="left" label="Menu">
        <div className="wm">
          <Link to="/" onClick={() => setMenu(false)}>
            litwatch
          </Link>
          <RunStatus />
        </div>
        <nav className="nav">
          <Tabs onPick={() => setMenu(false)} drawer />
          <Following onPick={() => setMenu(false)} />
        </nav>
        <nav className="foot nav">
          <Link to="/settings/$page" params={{ page: "overview" }} className="it" onClick={() => setMenu(false)}>
            ⚙ Settings
          </Link>
        </nav>
      </Sheet>
      <Toaster />
    </div>
  );
}

export function Shell() {
  return useDesktop() ? <Desktop /> : <Phone />;
}

/** "‹ Back" at the top of a deeper page on the desktop. */
export function Back() {
  const router = useRouter();
  const desktop = useDesktop();
  if (!desktop) return null;
  return (
    <button className="back" onClick={() => (router.history.canGoBack() ? router.history.back() : router.navigate({ to: "/" }))}>
      ‹ Back
    </button>
  );
}
