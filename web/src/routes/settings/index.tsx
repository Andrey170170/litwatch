import type { ReactNode } from "react";
import { Link, Navigate, Outlet, useRouterState } from "@tanstack/react-router";
import { ArrowLeft } from "lucide-react";
import { useDesktop } from "@/hooks/useDesktop";
import { cn } from "@/lib/utils";

type SettingsPath = "/settings/topics" | "/settings/quality" | "/settings/misses" | "/settings/runs" | "/settings/integrations" | "/settings/configuration";

export const GROUPS: { group: string; pages: { slug: string; to: SettingsPath; name: string; blurb: string }[] }[] = [
  {
    group: "Reading",
    pages: [
      { slug: "topics", to: "/settings/topics", name: "Topics", blurb: "Create, edit, order and pause topics; preview a draft; scoped re-runs." },
      { slug: "quality", to: "/settings/quality", name: "Quality", blurb: "What last night left, and how well the Watch topics are doing." },
      { slug: "misses", to: "/settings/misses", name: "Misses", blurb: "Reported misses with what each stage said." },
    ],
  },
  {
    group: "System",
    pages: [
      { slug: "runs", to: "/settings/runs", name: "Runs and cost", blurb: "A row per run with counts, duration, cost and errors." },
      { slug: "integrations", to: "/settings/integrations", name: "Integrations", blurb: "One card per module, and the outbox under them." },
      { slug: "configuration", to: "/settings/configuration", name: "Configuration", blurb: "The loaded litwatch.toml, secrets masked, read-only." },
    ],
  },
];

/** The kitchen: an index on the left, plain pages on the right, in the interface sans. */
export function SettingsLayout() {
  const desktop = useDesktop();
  const path = useRouterState({ select: (s) => s.location.pathname });
  if (!desktop) return <Outlet />;
  return (
    <div className="grid min-h-[calc(100dvh-49px)] grid-cols-[200px_minmax(0,1fr)]">
      <nav className="border-r border-border py-5 text-[14px]" aria-label="Settings">
        {GROUPS.map((g) => (
          <div key={g.group}>
            <div className="px-6 pt-3.5 pb-1 text-[11.5px] tracking-[.04em] text-muted-foreground uppercase">{g.group}</div>
            {g.pages.map((p) => (
              <Link
                key={p.slug}
                to={p.to}
                className={cn(
                  "block px-6 py-[7px] text-muted-foreground hover:text-foreground",
                  (path === `/settings/${p.slug}` || path.startsWith(`/settings/${p.slug}/`) || (p.slug === "quality" && path.startsWith("/settings/calibrate"))) &&
                    "font-semibold text-foreground shadow-[inset_3px_0_0_var(--primary)]",
                )}
              >
                {p.name}
              </Link>
            ))}
          </div>
        ))}
      </nav>
      <div className="min-w-0 px-8 py-6 text-[13.5px]">
        <Outlet />
      </div>
    </div>
  );
}

export function SettingsIndex() {
  const desktop = useDesktop();
  if (desktop) return <Navigate to="/settings/topics" replace />;
  return (
    <div className="px-4 py-4">
      <h1 className="font-serif text-[24px] font-semibold">Settings</h1>
      {GROUPS.map((g) => (
        <div key={g.group} className="mt-4">
          <div className="text-[11.5px] tracking-[.04em] text-muted-foreground uppercase">{g.group}</div>
          {g.pages.map((p) => (
            <Link key={p.slug} to={p.to} className="block border-b border-border py-3">
              <div className="text-[15px] font-medium">{p.name}</div>
              <div className="text-[13px] text-muted-foreground">{p.blurb}</div>
            </Link>
          ))}
        </div>
      ))}
    </div>
  );
}

/** A settings page's frame: back link on the phone, title, one line under it. */
export function Page({ title, sub, back, actions, children }: { title: string; sub?: ReactNode; back?: { to: string; label: string }; actions?: ReactNode; children: ReactNode }) {
  const b = back ?? { to: "/settings", label: "Settings" };
  return (
    <div className="px-4 py-3 text-[13.5px] desk:p-0">
      <Link to={b.to} className="hit -ml-2 justify-start gap-1 px-2 text-[13.5px] text-muted-foreground desk:hidden">
        <ArrowLeft className="size-4" aria-hidden /> {b.label}
      </Link>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="font-serif text-[24px] font-semibold">{title}</h1>
          {sub && <p className="mt-1 max-w-[640px] text-[13.5px] text-muted-foreground">{sub}</p>}
        </div>
        {actions}
      </div>
      <div className="mt-5">{children}</div>
    </div>
  );
}

export function H2({ children, note }: { children: ReactNode; note?: ReactNode }) {
  return (
    <h2 className="mt-7 mb-2 flex flex-wrap items-baseline gap-x-3 text-[15px] font-semibold first:mt-0">
      {children}
      {note && <span className="text-[12.5px] font-normal text-muted-foreground">{note}</span>}
    </h2>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <p className="rounded-lg border border-dashed border-border px-3.5 py-2.5 text-[13px] text-muted-foreground">{children}</p>;
}

export const BTN = "inline-flex min-h-[36px] items-center justify-center gap-1.5 rounded-full border border-border px-3.5 text-[13px] hover:bg-muted disabled:opacity-50 disabled:hover:bg-transparent";
export const BTN_PRIMARY = "inline-flex min-h-[36px] items-center justify-center gap-1.5 rounded-full bg-foreground px-4 text-[13px] font-medium text-background hover:opacity-90 disabled:opacity-50";
export const FIELD = "w-full rounded-lg border border-border bg-transparent px-3 py-2 text-[13.5px] text-foreground outline-none focus-visible:border-foreground";
export const TABLE = "w-full border-collapse text-[13px] [&_th]:border-b [&_th]:border-foreground [&_th]:py-1.5 [&_th]:pr-3 [&_th]:text-left [&_th]:text-[12px] [&_th]:font-medium [&_th]:text-muted-foreground [&_td]:border-b [&_td]:border-border [&_td]:py-2 [&_td]:pr-3 [&_td]:align-top";

export function pct(x: number | null | undefined): string {
  return x === null || x === undefined ? "–" : `${Math.round(x * 100)}%`;
}
export function money(x: number | string | null | undefined): string {
  if (x === null || x === undefined || x === "") return "–";
  const n = typeof x === "string" ? Number(x) : x;
  return Number.isFinite(n) ? `$${n.toFixed(2)}` : "–";
}
