import { useEffect, useMemo } from "react";
import { Link, useNavigate, useSearch } from "@tanstack/react-router";
import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { api, ok, type Entry as EntryT, type Issue, type IssueSummary, type Section as SectionT } from "@/api/client";
import { issueQuery } from "@/api/queries";
import { scheduleQuery } from "@/api/settings";
import { fireWords } from "@/lib/schedule";
import { PaperView } from "@/components/PaperView";
import { RecapView, ReviewView } from "@/components/Review";
import { GearLink } from "@/components/Shell";
import { StatusText } from "@/components/StatusChip";
import { Thumbs } from "@/components/Thumbs";
import { BibtexCopy, SaveToggle, arxivUrl, pdfUrl } from "@/components/paper-bits";
import { useDesktop } from "@/hooks/useDesktop";
import { authorsLine, day, fmt, localDay } from "@/lib/format";
import { topicColour } from "@/lib/topics";
import { cn } from "@/lib/utils";

const PAGE = 14;

function useFeed() {
  return useInfiniteQuery({
    queryKey: ["digest"],
    queryFn: async ({ pageParam }) =>
      ok(await api.GET("/api/digest", { params: { query: { before: pageParam ?? undefined, limit: PAGE } } })),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (last) => (last.issues.length === PAGE ? last.issues[last.issues.length - 1]?.day : undefined),
  });
}

/** The Digest: masthead, the pinned weekly row, the open issue, and earlier issues folded. */
export function DigestPage() {
  const desktop = useDesktop();
  const search = useSearch({ from: "/" });
  const feed = useFeed();
  const first = feed.data?.pages[0];
  const issues = useMemo(() => feed.data?.pages.flatMap((p) => p.issues) ?? [], [feed.data]);
  const weekly = first?.weekly ?? null;

  const newest = issues[0];
  // The issue asked for is loaded by id, whether or not the feed's first page lists it.
  const openId = search.issue ?? newest?.id;
  const open = useQuery({ ...issueQuery(openId ?? 0), enabled: openId !== undefined });
  const openDay = open.data?.day ?? [weekly, ...issues].find((i) => i?.id === openId)?.day;

  const today = localDay(new Date());
  // "nothing new since" when the newest issue is older than today and no older one was asked for.
  const stale = newest && !search.issue && newest.day < today ? newest.day : null;
  const mastDate = stale ? today : openDay;
  // A weekly issue is headed by its week, not the Monday it was built on.
  const openWeekly = !stale && (open.data?.kind ?? [weekly, ...issues].find((i) => i?.id === openId)?.kind) === "weekly";
  const mastText = mastDate ? (openWeekly ? fmt.week(mastDate) : fmt.issueDay(mastDate)) : "";

  return (
    <div className={cn(desktop && "grid min-h-[calc(100dvh-49px)] grid-cols-2")}>
      <div className={cn(desktop && "border-r border-border")}>
        {!desktop && <Masthead text={mastText} stale={stale} />}
        <div className="mx-auto max-w-[700px] px-4 pt-4 pb-10 desk:px-7 desk:pt-6">
          {desktop && mastDate && (
            <h1 className="font-serif text-[30px] leading-[1.1] font-semibold tracking-[-.015em]">
              {mastText}
              {stale && <span className="ml-3 font-sans text-[13px] font-normal text-muted-foreground">nothing new since {fmt.weekday(stale)}</span>}
            </h1>
          )}
          {weekly && <WeeklyRow weekly={weekly} open={openId === weekly.id} />}

          {feed.isPending && <p className="meta mt-6">Opening the paper…</p>}
          {feed.isError && <p className="mt-6 text-destructive">The Digest could not be loaded.</p>}
          {first && !newest && !weekly && <FirstIssue />}
          {open.isError && <p className="mt-6 text-destructive">No such issue.</p>}
          {open.data && <IssueView issue={open.data} openPaper={desktop ? search.paper : undefined} />}

          {issues.filter((i) => i.id !== openId).length > 0 && (
            <>
              <p className="mt-8 mb-1 text-[12px] tracking-[.06em] text-muted-foreground uppercase">Earlier</p>
              {issues
                .filter((i) => i.id !== openId)
                .map((i) => (
                  <FoldedIssue key={i.id} issue={i} />
                ))}
            </>
          )}
          {feed.hasNextPage && (
            <p className="mt-4 border-t border-border pt-3 text-[14px] text-muted-foreground">
              <button type="button" className="underline decoration-border hover:text-foreground" onClick={() => void feed.fetchNextPage()}>
                {feed.isFetchingNextPage ? "loading…" : "earlier"}
              </button>
            </p>
          )}
        </div>
      </div>
      {desktop && (
        <aside className="sticky top-[49px] max-h-[calc(100dvh-49px)] overflow-y-auto bg-card px-8 pt-6 pb-10">
          {search.paper ? (
            <PaperView id={search.paper} surfaceTopic={search.topic ?? paperTopic(open.data, search.paper)} compact />
          ) : (
            <p className="mt-10 text-center font-serif text-[16px] italic text-muted-foreground">Open an entry to read it here.</p>
          )}
        </aside>
      )}
    </div>
  );
}

/** Before any issue exists: when the first run is, if one is scheduled. */
function FirstIssue() {
  const schedule = useQuery(scheduleQuery);
  const s = schedule.data;
  const nightly = s?.enabled ? s.jobs.find((j) => j.kind === "nightly") : undefined;
  return (
    <p className="mt-8 font-serif text-[17px] text-body">
      No issue yet. The first one is written after the first nightly run
      {nightly ? `, ${fireWords(nightly.next, s!.zone)}` : ""}.
    </p>
  );
}

function paperTopic(issue: Issue | undefined, paperId: string): string | undefined {
  return issue?.sections.find((s) => s.entries.some((e) => e.paper_id === paperId))?.topic.slug;
}

/** The masthead on the phone: brand, a double rule, and the printer's line. */
function Masthead({ text, stale }: { text: string; stale: string | null }) {
  return (
    <div className="relative border-b-[3px] border-double border-foreground px-4 pt-[18px] pb-2.5 text-center">
      <span className="block font-serif text-[28px] leading-none font-bold tracking-[.06em]">Litwatch</span>
      <GearLink className="absolute top-2 right-2" />
      <div className="mt-2 flex flex-wrap justify-between gap-x-2.5 text-[11px] tracking-[.07em] text-muted-foreground uppercase">
        <span className="whitespace-nowrap">
          {text}
          {stale && ` · nothing new since ${fmt.weekday(stale)}`}
        </span>
        <StatusText uppercase />
      </div>
    </div>
  );
}

function WeeklyRow({ weekly, open }: { weekly: IssueSummary; open: boolean }) {
  return (
    <Link
      to="/"
      search={{ issue: weekly.id }}
      className={cn(
        "mt-3.5 flex items-baseline gap-2.5 rounded-[10px] border border-border bg-muted px-3.5 py-2.5 text-[13.5px]",
        open && "border-foreground",
      )}
    >
      <b className="font-serif text-[14.5px] whitespace-nowrap">This week</b>
      <span className="text-muted-foreground">{fmt.week(weekly.day)}</span>
      <span className="ml-auto text-[13px] text-muted-foreground">{open ? "Open" : "Read"}</span>
    </Link>
  );
}

function FoldedIssue({ issue }: { issue: IssueSummary }) {
  return (
    <Link to="/" search={{ issue: issue.id }} className="grid grid-cols-[1fr_auto] items-baseline gap-x-3.5 gap-y-1.5 border-t border-border py-3 hover:bg-muted/50">
      <span className="font-serif text-[16px] font-semibold">
        {fmt.issueDay(issue.day)}
        {issue.kind === "weekly" && <small className="ml-2 font-sans text-[12.5px] font-normal text-muted-foreground">weekly</small>}
      </span>
      <span className="text-[13px] text-muted-foreground">Open</span>
      {issue.lede && <span className="col-span-full text-[14px] text-body">{issue.lede}</span>}
    </Link>
  );
}

function IssueView({ issue, openPaper }: { issue: Issue; openPaper: string | undefined }) {
  const night = new Date(day(issue.day).getTime() - 86400000);
  return (
    <article>
      {issue.lede && <p className="mt-3 font-serif text-[16px] leading-[1.45] text-body desk:text-[17px]">{issue.lede}</p>}
      {issue.sections.map((s) => (
        <Section key={s.topic.slug} section={s} kind={issue.kind} openPaper={openPaper} />
      ))}
      {issue.sections.length === 0 && (
        <p className="mt-6 font-serif text-[16px] text-body">Nothing new for any topic.</p>
      )}
      <p className="mt-9 text-center font-serif italic text-muted-foreground">
        {issue.kind === "weekly"
          ? `That is the week of ${fmt.week(issue.day).replace("Week of ", "")}.`
          : `That is everything from the night of ${new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "long" }).format(night)}.`}
      </p>
    </article>
  );
}

/** One topic's section: entries for Watch, a review for Radar and Pulse, a recap in a weekly issue. */
function Section({ section, kind, openPaper }: { section: SectionT; kind: Issue["kind"]; openPaper: string | undefined }) {
  const colour = topicColour(section.topic.colour);
  const near = section.near ?? 0;
  const review = section.review ?? null;
  const recap = section.recap ?? null;
  const empty = section.entries.length === 0 && !review && !recap;
  return (
    <section className="mt-[26px]">
      <h2
        className="flex items-baseline gap-2.5 border-t-2 pt-[9px] font-serif text-[20px] font-semibold"
        style={{ borderColor: colour }}
      >
        <Link to="/topics/$slug" params={{ slug: section.topic.slug }}>
          {section.topic.name}
        </Link>
        {kind === "daily" && review && <small className="font-sans text-[13px] font-normal" style={{ color: colour }}>today's review</small>}
      </h2>
      {empty && <p className="mt-1.5 text-[14px] text-muted-foreground">Nothing new for {section.topic.name}.</p>}
      {recap && <RecapView recap={recap} topic={section.topic} surface="digest" />}
      {review && <ReviewView review={review} topic={section.topic} surface="digest" />}
      {section.entries.map((e) => (
        <EntryView key={e.paper_id} entry={e} open={openPaper === e.paper_id} />
      ))}
      {near > 0 && (
        <p className={cn("pt-3 text-[14px] text-muted-foreground", section.entries.length > 0 && "border-t border-border")}>
          <Link to="/topics/$slug" params={{ slug: section.topic.slug }} search={{ adjacent: true }} className="text-foreground underline decoration-border">
            {near} more
          </Link>{" "}
          near {section.topic.name}.
        </p>
      )}
    </section>
  );
}

/** One paper on the Digest (DESIGN.md "Entry"). */
function EntryView({ entry, open }: { entry: EntryT; open: boolean }) {
  const desktop = useDesktop();
  const navigate = useNavigate({ from: "/" });
  const meta = [authorsLine(entry.authors), entry.revised ? "revised" : fmt.published(entry.published)].filter(Boolean);
  const title = desktop ? (
    <Link to="/" search={(prev) => ({ ...prev, paper: entry.paper_id, topic: entry.topic })} className="hover:underline decoration-border">
      {entry.title}
    </Link>
  ) : (
    <Link to="/paper/$" params={{ _splat: entry.paper_id }} search={{ topic: entry.topic }}>
      {entry.title}
    </Link>
  );
  return (
    <div
      className={cn(
        "border-t border-border py-[15px] pb-[13px]",
        open && "-mx-3.5 rounded-lg border-t-transparent bg-muted px-3.5",
      )}
    >
      <h3
        className={cn(
          "font-serif leading-[1.3] font-semibold",
          entry.lead ? "text-[17px] desk:text-[19px]" : "text-[15.5px] desk:text-[16.5px]",
          entry.set_aside && "struck text-muted-foreground",
        )}
      >
        {title}
      </h3>
      <p className="meta mt-[3px] mb-2">
        {meta.join(" · ")}
        {entry.set_aside && " · set aside"}
        {entry.also_in && entry.also_in.length > 0 && ` · also in ${entry.also_in.join(", ")}`}
      </p>
      {entry.take && (
        <p className={cn("mb-2.5 font-serif text-[15.5px] leading-[1.5] text-body", entry.set_aside && "text-muted-foreground")}>
          {entry.take}
        </p>
      )}
      <div className="flex flex-wrap items-center gap-x-3.5 text-[13px] text-muted-foreground">
        {!desktop && (
          <button
            type="button"
            className="py-1.5 font-semibold text-primary"
            onClick={() => void navigate({ to: "/paper/$", params: { _splat: entry.paper_id }, search: { topic: entry.topic } })}
          >
            Read
          </button>
        )}
        <a href={arxivUrl(entry.paper_id)} target="_blank" rel="noreferrer" className="py-1.5 hover:text-foreground">
          arXiv
        </a>
        <a href={pdfUrl(entry.paper_id)} target="_blank" rel="noreferrer" className="py-1.5 hover:text-foreground">
          PDF
        </a>
        <SaveToggle paperId={entry.paper_id} saved={!!entry.saved} />
        <BibtexCopy ids={[entry.paper_id]} />
        <Thumbs
          key={`${entry.paper_id}:${entry.topic}`}
          className="ml-auto"
          paperId={entry.paper_id}
          topic={entry.topic}
          value={entry.reaction}
          reason={entry.reaction_reason}
        />
      </div>
    </div>
  );
}

/** Keep the open issue in view after a reaction refreshes it. */
export function useKeepIssue(id: number | undefined) {
  useEffect(() => {}, [id]);
}
