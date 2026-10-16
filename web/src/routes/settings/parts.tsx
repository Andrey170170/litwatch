/** The pieces Settings' pages share: the head, a paper link that keeps the Settings sidebar, the segmented
 * Fit control, a form field, a share as a bar, the states of a load, and the topic cards by slug. */
import { useQuery, type UseQueryResult } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import type { ReactNode } from "react";
import { summaryQuery } from "@/api/queries";
import { FITS, Seg } from "@/components/Seg";
import { Back } from "@/components/Shell";
import { Fill } from "@/components/slots";
import { level, type LabelValue } from "@/lib/settings";

/** The page's head: the phone's title, "‹ Back" on a page inside a page, the title, a line under it, and the actions
 * at the right, which stack under on the phone. */
export function PageHead({ title, sub, deeper, children }: { title: string; sub?: ReactNode; deeper?: boolean; children?: ReactNode }) {
  return (
    <>
      <Fill name="title">
        <span className="t">{title}</span>
      </Fill>
      {deeper && <Back />}
      <div className="sethd">
        <div>
          <div className="h1">{title}</div>
          {sub && <div className="tsub">{sub}</div>}
        </div>
        {children && <div className="nact">{children}</div>}
      </div>
    </>
  );
}

/** A link to a paper from Settings: the paper keeps the Settings sidebar (DESIGN.md, "Around the pages"). */
export function PaperLink({ id, topic, className, children }: { id: string; topic?: string | null; className?: string; children: ReactNode }) {
  return (
    <Link to="/paper/$" params={{ _splat: id }} search={topic ? { topic } : {}} state={{ settings: true }} className={className}>
      {children}
    </Link>
  );
}

/** The Fit control as five equal segments. */
/** The Fit control over the five label values; with `clear`, `onPick` gets null when the chosen value is tapped
 * again. */
export function FitSeg({ value, auto, onPick, disabled, label, clear }: { value: LabelValue | null | undefined; auto?: LabelValue | null; onPick: (f: LabelValue | null) => void; disabled?: boolean; label: string; clear?: boolean }) {
  return <Seg values={FITS} value={value} auto={auto} disabled={disabled} label={label} clear={clear} onPick={(f) => (f || clear) && onPick(f)} />;
}

/** A form field: its label, a hint after it, the control under. */
export function Field({ label, hint, children }: { label: string; hint?: string; children: ReactNode }) {
  return (
    <div className="fld">
      <div className="fl">
        {label}
        {hint && <span className="faint"> · {hint}</span>}
      </div>
      {children}
    </div>
  );
}

/** A share as a bar and its number, coloured by how good it is. */
export function Share({ x, hi, good, ok }: { x: number; hi?: boolean; good?: number; ok?: number }) {
  const lvl = level(x, { hi, good, ok });
  const pct = Math.round(x * 100);
  return (
    <>
      <span className={`sh-b ${lvl}`} aria-hidden>
        <i style={{ width: `${pct}%` }} />
      </span>
      <span className={`sh-n ${lvl}`}>{pct}%</span>
    </>
  );
}

/** A query that failed, with the way to try again; "Loading…" while it loads with nothing to show; a quiet
 * "Updating…" while a fresh visit refetches it behind its cached rows; nothing otherwise. */
export function Load({ q }: { q: Pick<UseQueryResult, "isError" | "isPending" | "isFetching" | "refetch"> }) {
  if (q.isError) return <Failed q={q} what="this" />;
  if (q.isPending) return <div className="loadmore">Loading…</div>;
  return q.isFetching ? <div className="loadmore quiet">Updating…</div> : null;
}

/** A read beside the page's own that failed (counts, the schedule, the classes a form offers): what it would have
 * shown is named, with the way to try again, so its absence never passes for content; nothing otherwise. */
export function Failed({ q, what }: { q: Pick<UseQueryResult, "isError" | "refetch">; what: string }) {
  if (!q.isError) return null;
  return (
    <div className="loadmore">
      Could not load {what} ·{" "}
      <button className="inl" onClick={() => void q.refetch()}>
        Try again
      </button>
    </div>
  );
}

/** A line that says the surface waits on an endpoint a later PR brings. */
export function Later({ children }: { children: ReactNode }) {
  return <div className="later">{children}</div>;
}

/** The followed topics' cards by slug, for a badge or a name beside a row. */
export function useTopicCards() {
  const { data } = useQuery({ ...summaryQuery, select: (s) => Object.fromEntries(s.topics.map((t) => [t.slug, t])) });
  return data ?? {};
}
