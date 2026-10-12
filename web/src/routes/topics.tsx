import { useEffect, useMemo } from "react";
import { Link, Navigate, Outlet, useNavigate, useParams, useSearch } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import { ArrowLeft, Settings, SlidersHorizontal } from "lucide-react";
import type { TopicCard as Card, TopicKind } from "@/api/client";
import { useVisit } from "@/api/mutations";
import { topicsQuery } from "@/api/queries";
import { Chip, PapersTable, SearchBox, YearChip } from "@/components/PapersTable";
import { Sheet, SheetContent, SheetTitle, SheetTrigger } from "@/components/ui/sheet";
import { useDesktop } from "@/hooks/useDesktop";
import { fmt, plural } from "@/lib/format";
import type { TableSearch } from "@/lib/search";
import { KIND_LABEL, KIND_ORDER, cadence, topicColour } from "@/lib/topics";
import { cn } from "@/lib/utils";

const LAST_OPEN = "litwatch.topic";

function grouped(cards: Card[]) {
  const by: Record<TopicKind, Card[]> = { watch: [], radar: [], pulse: [] };
  for (const c of cards) by[c.kind].push(c);
  // Paused topics sink within their group.
  for (const k of KIND_ORDER) by[k].sort((a, b) => Number(a.status !== "active") - Number(b.status !== "active"));
  return by;
}

/** What is new on a card: entries in issues since the topic was last opened. */
export function newLine(c: Card): { text: string; quiet: boolean } {
  if (c.status !== "active") return { text: "Paused", quiet: true };
  if (c.new === 0) return { text: "Nothing new", quiet: true };
  const since = c.opened_at ? ` since ${fmt.weekday(c.opened_at.slice(0, 10))}` : "";
  const saved = c.new_saved ? `, ${c.new_saved} saved` : "";
  return { text: `${c.new} new${since}${saved}`, quiet: false };
}

/** Desktop: the rail with the chosen topic open beside it. Phone: the pages under it. */
export function TopicsLayout() {
  const desktop = useDesktop();
  const topics = useQuery(topicsQuery);
  if (!desktop) return <Outlet />;
  const by = grouped(topics.data ?? []);
  return (
    <div className="grid min-h-[560px] grid-cols-[230px_minmax(0,1fr)]">
      <nav className="border-r border-border py-4 text-[14px]" aria-label="Topics">
        {KIND_ORDER.filter((k) => by[k].length).map((k) => (
          <div key={k}>
            <p className="mt-4 px-5 text-[11.5px] tracking-[.06em] text-muted-foreground uppercase first:mt-0">{KIND_LABEL[k]}</p>
            {by[k].map((c) => (
              <RailEntry key={c.slug} card={c} />
            ))}
          </div>
        ))}
      </nav>
      <div className="px-7 pt-6 pb-9">
        <Outlet />
      </div>
    </div>
  );
}

function RailEntry({ card: c }: { card: Card }) {
  const paused = c.status !== "active";
  return (
    <Link
      to="/topics/$slug"
      params={{ slug: c.slug }}
      className={cn("grid grid-cols-[3px_1fr_auto] items-baseline gap-x-3 py-2 pr-[18px] hover:bg-muted/60", paused && "text-muted-foreground")}
      activeProps={{ className: "bg-muted font-semibold [&>span:first-child]:bg-(--tc)" }}
      style={{ "--tc": topicColour(c.colour, c.status) } as React.CSSProperties}
    >
      <span className="h-full w-[3px] self-stretch rounded-r-[2px]" aria-hidden />
      <span>{c.name}</span>
      {c.new > 0 ? (
        <span className="tabular text-[12px] font-semibold">{c.new} new</span>
      ) : (
        <span className="text-[12px] font-normal text-muted-foreground">{cadence(c.kind) ?? "nightly"}</span>
      )}
    </Link>
  );
}

/** Phone: one screen of cards. Desktop: land on the topic last open. */
export function TopicsIndex() {
  const desktop = useDesktop();
  const topics = useQuery(topicsQuery);
  if (desktop) {
    if (!topics.data) return null;
    const last = localStorage.getItem(LAST_OPEN);
    const slug = topics.data.find((t) => t.slug === last)?.slug ?? topics.data[0]?.slug;
    if (!slug) return <p className="meta p-7">No topics yet.</p>;
    return <Navigate to="/topics/$slug" params={{ slug }} replace />;
  }
  const by = grouped(topics.data ?? []);
  return (
    <div className="px-4 pt-3.5 pb-8">
      {KIND_ORDER.filter((k) => by[k].length).map((k) => (
        <div key={k}>
          <p className="mt-[18px] mb-0.5 text-[11.5px] tracking-[.06em] text-muted-foreground uppercase first:mt-0">{KIND_LABEL[k]}</p>
          {by[k].map((c) => (
            <TopicCardView key={c.slug} card={c} />
          ))}
        </div>
      ))}
    </div>
  );
}

function TopicCardView({ card: c }: { card: Card }) {
  const line = newLine(c);
  const paused = c.status !== "active";
  const unit = c.kind === "watch" ? "paper" : "pick";
  return (
    <Link
      to="/topics/$slug"
      params={{ slug: c.slug }}
      className={cn("my-2.5 grid grid-cols-[4px_1fr_auto] items-center gap-x-3.5 rounded-[10px] border border-border py-3 pr-3.5", paused && "opacity-55")}
    >
      <span className="h-full w-1 self-stretch rounded-r-[3px]" style={{ background: topicColour(c.colour, c.status) }} aria-hidden />
      <div>
        <div className="font-serif text-[17px] leading-[1.2] font-semibold">
          {c.name}
          <small className="ml-2 font-sans text-[12px] font-normal tracking-[.02em] text-muted-foreground">
            {KIND_LABEL[c.kind]}
            {cadence(c.kind) ? ` · ${cadence(c.kind)}` : ""}
            {paused ? " · paused" : ""}
          </small>
        </div>
        <div className={cn("mt-[3px] text-[13.5px] text-body", line.quiet && "text-muted-foreground")}>{line.text}</div>
      </div>
      <div className="tabular text-right text-[12.5px] leading-[1.4] whitespace-nowrap text-muted-foreground">
        {plural(c.papers, unit)}
        {c.saved > 0 && (
          <>
            <br />
            {c.saved} saved
          </>
        )}
      </div>
    </Link>
  );
}

/** A topic page: the table with the topic fixed (DESIGN.md "Topics"). */
export function TopicPage() {
  const desktop = useDesktop();
  const { slug } = useParams({ from: "/topics/$slug" });
  const search = useSearch({ from: "/topics/$slug" });
  const navigate = useNavigate({ from: "/topics/$slug" });
  const topics = useQuery(topicsQuery);
  const visit = useVisit();
  const card = topics.data?.find((t) => t.slug === slug);
  const watch = useMemo(() => topics.data?.filter((t) => t.kind === "watch") ?? [], [topics.data]);

  useEffect(() => {
    localStorage.setItem(LAST_OPEN, slug);
    visit.mutate(slug);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [slug]);

  const onFilter = (patch: Partial<TableSearch>) =>
    void navigate({ search: (prev) => ({ ...prev, page: undefined, ...patch }) });

  if (topics.data && !card) return <p className="p-4 text-destructive">No such topic.</p>;
  const colour = card ? topicColour(card.colour, card.status) : undefined;
  const picks = card && card.kind !== "watch";
  const filters = (
    <>
      <Chip on={!search.saved && !search.adjacent} onClick={() => onFilter({ saved: undefined, adjacent: undefined })}>
        All
      </Chip>
      <Chip on={search.saved === true} onClick={() => onFilter({ saved: search.saved ? undefined : true })}>
        Saved
      </Chip>
      {!picks && (
        <Chip on={!!search.adjacent} onClick={() => onFilter({ adjacent: search.adjacent ? undefined : true })}>
          Also adjacent
        </Chip>
      )}
      <YearChip year={search.year} onChange={(year) => onFilter({ year })} />
    </>
  );

  return (
    <div className={cn(!desktop && "px-4 pb-8")}>
      {!desktop && (
        <nav className="-mx-1 flex items-center justify-between py-1">
          <Link to="/topics" className="hit justify-start gap-1 px-1 text-[13.5px] text-muted-foreground">
            <ArrowLeft className="size-4" aria-hidden /> Topics
          </Link>
          <EditGear />
        </nav>
      )}
      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="font-serif text-[28px] leading-[1.1] font-semibold tracking-[-.015em] desk:text-[30px]" style={{ color: colour }}>
            {card?.name ?? "…"}
          </h1>
          {card && (
            <p className="mt-2 max-w-[640px] text-[14.5px] text-body">
              {card.subject} <span className="text-muted-foreground">{plural(card.papers, picks ? "pick" : "paper")}, {card.saved} saved.</span>
            </p>
          )}
        </div>
        {desktop && <EditGear />}
      </div>
      {picks && (
        <p className="mt-4 rounded-lg border border-dashed border-border px-3.5 py-2.5 text-[13px] text-muted-foreground">
          Reviews arrive with a later stage; the picks are below.
        </p>
      )}
      <div className="mt-[18px] flex flex-wrap items-center gap-2 text-[13px]">
        <SearchBox value={search.q} onChange={(q) => onFilter({ q })} placeholder={`Search within ${card?.name ?? "the topic"}`} />
        {desktop ? (
          filters
        ) : (
          <FilterSheet>{filters}</FilterSheet>
        )}
      </div>
      {card && (
        <PapersTable filter={{ ...search, topic: slug }} onFilter={onFilter} watchTopics={watch} fixedTopic={card} />
      )}
    </div>
  );
}

function EditGear() {
  return (
    <Link to="/settings/$page" params={{ page: "topics" }} aria-label="Edit this topic in Settings" className="hit rounded-full text-muted-foreground hover:text-foreground">
      <span className="grid size-[26px] place-items-center rounded-full border border-border">
        <Settings className="size-[14px]" strokeWidth={1.75} aria-hidden />
      </span>
    </Link>
  );
}

/** On the phone the filters sit in a sheet behind one button. */
export function FilterSheet({ children }: { children: React.ReactNode }) {
  return (
    <Sheet>
      <SheetTrigger asChild>
        <button type="button" className="inline-flex min-h-[36px] items-center gap-1.5 rounded-full border border-border px-3 text-[13px] text-muted-foreground">
          <SlidersHorizontal className="size-4" aria-hidden /> Filter
        </button>
      </SheetTrigger>
      <SheetContent side="bottom" className="rounded-t-2xl border-border bg-background pb-[calc(24px+env(safe-area-inset-bottom))]">
        <SheetTitle className="font-serif text-[18px]">Filter and sort</SheetTitle>
        <div className="mt-3 flex flex-wrap gap-2">{children}</div>
      </SheetContent>
    </Sheet>
  );
}
