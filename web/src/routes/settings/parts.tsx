/** The pieces Settings' pages share: the head, a paper link that keeps the Settings sidebar, the Fit segments, a
 * number table's cells, and the states of a load. */
import { Link, type LinkProps } from "@tanstack/react-router";
import type { ReactNode } from "react";
import { Back } from "@/components/Shell";
import { Fill } from "@/components/slots";
import { level, type LabelValue } from "@/lib/settings";

export const FITS: LabelValue[] = ["central", "related", "adjacent", "none", "unsure"];

/** The page's head: the phone's title, "‹ Back" on a page inside a page, the title, a line under it, and the actions
 * at the right, which stack under on the phone. */
export function PageHead({ title, sub, deeper, phoneTitle, children }: { title: string; sub?: ReactNode; deeper?: boolean; phoneTitle?: string; children?: ReactNode }) {
  return (
    <>
      <Fill name="title">
        <span className="t">{phoneTitle ?? title}</span>
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
    <Link to="/paper/$" params={{ _splat: id }} search={topic ? { topic } : {}} state={{ settings: true } as LinkProps["state"]} className={className}>
      {children}
    </Link>
  );
}

/** The Fit control as five equal segments: the owner's word filled, the pipeline's underlined while unanswered. */
export function FitSeg({ value, auto, onPick, disabled, label }: { value: LabelValue | null | undefined; auto?: LabelValue | null; onPick: (f: LabelValue) => void; disabled?: boolean; label: string }) {
  return (
    <div className="fitseg" role="group" aria-label={label}>
      {FITS.map((f) => (
        <button key={f} className={`seg${value === f ? " on" : ""}${!value && auto === f ? " auto" : ""}`} aria-pressed={value === f} disabled={disabled} onClick={() => onPick(f)}>
          {f}
        </button>
      ))}
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

/** A query that failed, with the way to try again; nothing while it loads. */
export function Load({ error, retry, loading }: { error: boolean; retry: () => void; loading?: boolean }) {
  if (error)
    return (
      <div className="loadmore">
        Could not load this ·{" "}
        <button className="inl" onClick={retry}>
          Try again
        </button>
      </div>
    );
  return loading ? <div className="loadmore">Loading…</div> : null;
}

/** A line that says the surface waits on an endpoint a later PR brings. */
export function Later({ children }: { children: ReactNode }) {
  return <div className="later">{children}</div>;
}
