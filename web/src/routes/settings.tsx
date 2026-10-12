import { Link, Navigate, Outlet, useMatch, useParams } from "@tanstack/react-router";
import { ArrowLeft } from "lucide-react";
import { useDesktop } from "@/hooks/useDesktop";
import { cn } from "@/lib/utils";

const GROUPS: { group: string; pages: { slug: string; name: string; blurb: string }[] }[] = [
  {
    group: "Reading",
    pages: [
      { slug: "topics", name: "Topics", blurb: "Create, edit, order and pause topics; preview a draft; scoped re-runs." },
      { slug: "quality", name: "Quality", blurb: "Last night's disagreements, ungraded candidates and failures; Calibrate; recall and precision." },
      { slug: "misses", name: "Misses", blurb: "Reported misses with what each stage said." },
    ],
  },
  {
    group: "System",
    pages: [
      { slug: "runs", name: "Runs and cost", blurb: "A row per night with counts, duration, cost and errors." },
      { slug: "integrations", name: "Integrations", blurb: "One card per module, and the outbox under them." },
      { slug: "configuration", name: "Configuration", blurb: "The loaded litwatch.toml, secrets masked, read-only." },
    ],
  },
];

/** The kitchen: an index on the left, plain pages on the right, in the interface sans. */
export function SettingsPage() {
  const desktop = useDesktop();
  const match = useMatch({ from: "/settings/$page", shouldThrow: false });
  if (!desktop) return <Outlet />;
  return (
    <div className="grid min-h-[480px] grid-cols-[200px_minmax(0,1fr)]">
      <div className="border-r border-border py-5 text-[14px]">
        {GROUPS.map((g) => (
          <div key={g.group}>
            <div className="px-6 pt-3.5 pb-1 text-[11.5px] tracking-[.04em] text-muted-foreground uppercase">{g.group}</div>
            {g.pages.map((p) => (
              <Link
                key={p.slug}
                to="/settings/$page"
                params={{ page: p.slug }}
                className={cn(
                  "block px-6 py-[7px] text-muted-foreground hover:text-foreground",
                  match?.params.page === p.slug && "font-semibold text-foreground shadow-[inset_3px_0_0_var(--primary)]",
                )}
              >
                {p.name}
              </Link>
            ))}
          </div>
        ))}
      </div>
      <div className="px-8 py-6">
        <Outlet />
      </div>
    </div>
  );
}

export function SettingsIndex() {
  const desktop = useDesktop();
  if (desktop) return <Navigate to="/settings/$page" params={{ page: "topics" }} replace />;
  return (
    <div className="px-4 py-4">
      <h1 className="font-serif text-[24px] font-semibold">Settings</h1>
      {GROUPS.map((g) => (
        <div key={g.group} className="mt-4">
          <div className="text-[11.5px] tracking-[.04em] text-muted-foreground uppercase">{g.group}</div>
          {g.pages.map((p) => (
            <Link key={p.slug} to="/settings/$page" params={{ page: p.slug }} className="block border-b border-border py-3">
              <div className="text-[15px] font-medium">{p.name}</div>
              <div className="text-[13px] text-muted-foreground">{p.blurb}</div>
            </Link>
          ))}
        </div>
      ))}
    </div>
  );
}

export function SettingsPlaceholder() {
  const { page } = useParams({ from: "/settings/$page" });
  const found = GROUPS.flatMap((g) => g.pages).find((p) => p.slug === page);
  return (
    <div className="px-4 py-3 desk:p-0">
      <Link to="/settings" className="hit -ml-2 justify-start gap-1 px-2 text-[13.5px] text-muted-foreground desk:hidden">
        <ArrowLeft className="size-4" aria-hidden /> Settings
      </Link>
      <h1 className="font-serif text-[24px] font-semibold">{found?.name ?? "Settings"}</h1>
      <p className="mt-1 text-[13.5px] text-muted-foreground">{found?.blurb}</p>
      <p className="mt-6 rounded-lg border border-dashed border-border px-3.5 py-2.5 text-[13px] text-muted-foreground">
        This page arrives with a later stage.
      </p>
    </div>
  );
}
