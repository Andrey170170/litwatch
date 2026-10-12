import { useEffect, useState } from "react";
import { Link, Outlet, useRouterState } from "@tanstack/react-router";
import { useQueryClient } from "@tanstack/react-query";
import { BookOpen, Layers, Newspaper, Settings } from "lucide-react";
import { onUnauthorised } from "@/api/client";
import { StatusChip } from "@/components/StatusChip";
import { TokenPrompt } from "@/components/TokenPrompt";
import { useToasts } from "@/lib/toast";
import { cn } from "@/lib/utils";

const PLACES = [
  { to: "/", label: "Digest", icon: Newspaper },
  { to: "/topics", label: "Topics", icon: Layers },
  { to: "/papers", label: "Papers", icon: BookOpen },
] as const;

/** The frame: three words and a gear in the header on a desktop, a three-button footer on the phone. */
export function Shell() {
  const [locked, setLocked] = useState(false);
  const qc = useQueryClient();
  const path = useRouterState({ select: (s) => s.location.pathname });
  const onDigest = path === "/";
  const onPaper = path.startsWith("/paper/");

  useEffect(() => onUnauthorised(() => setLocked(true)), []);

  return (
    <div className="flex min-h-dvh flex-col">
      {/* Phone: the Digest has its masthead, the paper page its back link; the others a plain header. */}
      <header
        className={cn(
          "sticky top-0 z-20 flex items-center gap-[22px] border-b border-border bg-background/95 px-4 py-[11px] backdrop-blur desk:px-7",
          (onDigest || onPaper) && "max-desk:hidden",
        )}
      >
        <Link to="/" className="font-serif text-[16px] font-semibold">
          litwatch
        </Link>
        <nav className="hidden items-center gap-[22px] text-[13.5px] text-muted-foreground desk:flex" aria-label="Places">
          {PLACES.map((p) => (
            <Link
              key={p.to}
              to={p.to}
              className="py-1 hover:text-foreground"
              activeOptions={{ exact: p.to === "/", includeSearch: false }}
              activeProps={{ className: "text-foreground font-semibold" }}
            >
              {p.label}
            </Link>
          ))}
          <StatusChip className="ml-1" />
        </nav>
        <GearLink className="ml-auto" active={path.startsWith("/settings")} />
      </header>

      <main className="flex-1 pb-[calc(64px+env(safe-area-inset-bottom))] desk:pb-0">
        <Outlet />
      </main>

      <nav
        className="fixed inset-x-0 bottom-0 z-20 flex justify-around border-t border-border bg-background/95 pb-[env(safe-area-inset-bottom)] backdrop-blur desk:hidden"
        aria-label="Places"
      >
        {PLACES.map((p) => (
          <Link
            key={p.to}
            to={p.to}
            className="flex min-w-[88px] flex-col items-center gap-0.5 px-3 pt-2 pb-2.5 text-[11.5px] text-muted-foreground"
            activeOptions={{ exact: p.to === "/", includeSearch: false }}
            activeProps={{ className: "text-foreground font-semibold" }}
          >
            <p.icon className="size-5" strokeWidth={1.75} aria-hidden />
            {p.label}
          </Link>
        ))}
      </nav>

      <Toaster />
      {locked && (
        <TokenPrompt
          onSignedIn={() => {
            setLocked(false);
            void qc.invalidateQueries();
          }}
        />
      )}
    </div>
  );
}

export function GearLink({ className, active }: { className?: string; active?: boolean }) {
  return (
    <Link
      to="/settings"
      aria-label="Settings"
      className={cn(
        "hit rounded-full text-muted-foreground hover:text-foreground",
        active && "text-foreground",
        className,
      )}
    >
      <span className="grid size-[26px] place-items-center rounded-full border border-border">
        <Settings className="size-[14px]" strokeWidth={1.75} aria-hidden />
      </span>
    </Link>
  );
}

function Toaster() {
  const toasts = useToasts();
  return (
    <div
      className="pointer-events-none fixed inset-x-0 bottom-[calc(72px+env(safe-area-inset-bottom))] z-40 flex flex-col items-center gap-2 px-4 desk:bottom-6"
      aria-live="polite"
    >
      {toasts.map((t) => (
        <div
          key={t.id}
          className={cn(
            "animate-in fade-in slide-in-from-bottom-2 rounded-full border border-border bg-foreground px-4 py-2 text-[13.5px] text-background shadow-md",
            t.tone === "error" && "bg-destructive text-destructive-foreground",
          )}
        >
          {t.text}
        </div>
      ))}
    </div>
  );
}
