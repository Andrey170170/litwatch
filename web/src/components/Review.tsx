/**
 * Reviews and recaps: litwatch's field notes on a Radar or Pulse topic and its weekly round-up
 * of a Watch topic, as prose with the picks and lists under it (SPEC §3 "Weekly", DESIGN.md
 * "Topics"). A failed one is one line, filled in when the review is run again.
 */
import { Link } from "@tanstack/react-router";
import type { Listed, Pick as PickT, Recap, Review, TopicRef } from "@/api/client";
import { SaveToggle } from "@/components/paper-bits";
import { useDesktop } from "@/hooks/useDesktop";
import { authorsLine, fmt, plural } from "@/lib/format";
import { cn } from "@/lib/utils";

/** Where a paper opens from here: the Digest's reading pane on a desktop, else its page. */
export type Surface = "digest" | "topic";

/** Review prose: paragraphs split on blank lines. */
export function Prose({ text, className }: { text: string; className?: string }) {
  return (
    <div className={cn("font-serif text-[16px] leading-[1.55] text-body [&>p+p]:mt-3", className)}>
      {text
        .split(/\n\s*\n/)
        .map((p) => p.trim())
        .filter(Boolean)
        .map((p, i) => (
          <p key={i}>{p}</p>
        ))}
    </div>
  );
}

function PaperLink({ id, title, topic, surface, className }: { id: string; title: string; topic: string; surface: Surface; className?: string }) {
  const desktop = useDesktop();
  if (surface === "digest" && desktop)
    return (
      <Link to="/" search={(prev) => ({ ...prev, paper: id, topic })} className={cn("hover:underline decoration-border", className)}>
        {title}
      </Link>
    );
  return (
    <Link to="/paper/$" params={{ _splat: id }} search={{ topic }} className={className}>
      {title}
    </Link>
  );
}

const QUIET = "mt-1.5 text-[14px] text-muted-foreground";

/** A Radar or Pulse review: the prose, then the picks with why each was picked. */
export function ReviewView({ review, topic, surface }: { review: Review; topic: TopicRef; surface: Surface }) {
  const span = review.period === "day" ? "day" : "week";
  if (review.state === "failed")
    return (
      <p className={QUIET}>
        The {span}'s review of {plural(review.members ?? 0, "paper")} did not get written; litwatch tries again on the next run.
      </p>
    );
  if (review.state === "quiet" || !review.text)
    return <p className={QUIET}>Nothing joined {topic.name} this {span}.</p>;
  const picks = review.picks ?? [];
  return (
    <div className="mt-2.5">
      <Prose text={review.text} />
      {picks.length > 0 && (
        <ol className="mt-3 border-t border-border">
          {picks.map((p) => (
            <PickView key={p.paper_id} pick={p} topic={topic.slug} surface={surface} />
          ))}
        </ol>
      )}
    </div>
  );
}

/** One pick: title, meta line, why it was picked, and Save (decision 3: picks are saved by hand). */
function PickView({ pick, topic, surface }: { pick: PickT; topic: string; surface: Surface }) {
  const meta = [authorsLine(pick.authors ?? []), fmt.published(pick.published)].filter(Boolean).join(" · ");
  return (
    <li className="border-b border-border py-[11px]">
      <h4 className="font-serif text-[15.5px] leading-[1.3] font-semibold desk:text-[16.5px]">
        <PaperLink id={pick.paper_id} title={pick.title} topic={topic} surface={surface} />
      </h4>
      <p className="meta mt-[2px] flex flex-wrap items-baseline gap-x-3">
        <span>{meta}</span>
        <SaveToggle paperId={pick.paper_id} saved={!!pick.saved} />
      </p>
      <p className="mt-1 font-serif text-[15px] leading-[1.5] text-body">{pick.why}</p>
    </li>
  );
}

/** A Watch topic's weekly recap: the prose, then what was announced, set aside and missed. */
export function RecapView({ recap, topic, surface }: { recap: Recap; topic: TopicRef; surface: Surface }) {
  if (recap.state === "failed")
    return <p className={QUIET}>The week's recap of {topic.name} did not get written; litwatch tries again on the next run.</p>;
  if (recap.state === "quiet" || !recap.text) return <p className={QUIET}>Nothing was announced for {topic.name} this week.</p>;
  return (
    <div className="mt-2.5">
      <Prose text={recap.text} />
      <Listing label="Announced this week" items={recap.papers ?? []} topic={topic.slug} surface={surface} />
      <Listing label="Set aside" items={recap.set_aside ?? []} topic={topic.slug} surface={surface} struck />
      <Listing label="Reported as missed" items={recap.misses ?? []} topic={topic.slug} surface={surface} />
    </div>
  );
}

function Listing({ label, items, topic, surface, struck }: { label: string; items: Listed[]; topic: string; surface: Surface; struck?: boolean }) {
  if (items.length === 0) return null;
  return (
    <div className="mt-3.5">
      <p className="text-[11.5px] tracking-[.06em] text-muted-foreground uppercase">
        {label} · {items.length}
      </p>
      <ul className="mt-1 border-t border-border">
        {items.map((p) => (
          <li key={p.paper_id} className={cn("border-b border-border py-[7px] font-serif text-[15px] leading-[1.3] font-semibold", struck && "struck text-muted-foreground")}>
            <PaperLink id={p.paper_id} title={p.title} topic={topic} surface={surface} />
          </li>
        ))}
      </ul>
    </div>
  );
}
