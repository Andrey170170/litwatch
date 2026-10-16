/** A topic's digest strip (DESIGN.md, anatomy): its recent digests as squares in their best rarity, newest at the
 * right, each opening its digest; an empty square for nothing new, a red × for a failed one. Where it is too
 * narrow, whole squares of the oldest drop out. */
import { Link } from "@tanstack/react-router";
import type { Schemas } from "@/api/client";
import { dayLabel, weekRange } from "@/lib/digests";
import { rc } from "@/lib/rarity";

export function Strip({ strip, depth, small }: { strip: Schemas["StripDigest"][] | undefined; depth: Schemas["Depth"]; small?: boolean }) {
  if (!strip?.length) return null;
  const weekly = depth === "field";
  return (
    <div className={`strip${small ? " sm" : ""}`}>
      <span>{weekly ? "Weekly digests" : "Daily digests"}</span>
      <span className="sq">
        {[...strip].reverse().map((d) => (
          <Link
            key={d.id}
            to="/digests/$id"
            params={{ id: String(d.id) }}
            className={d.state === "nothing_new" ? "h" : d.state === "failed" ? "x" : ""}
            style={{ "--rc": rc(d.best_rarity) } as React.CSSProperties}
            title={`${weekly ? `Week ${weekRange(d.day)}` : dayLabel(d.day)} · ${d.state === "nothing_new" ? "Nothing new" : d.state === "failed" ? "Could not be written" : (d.best_rarity ?? "")}`}
            onClick={(e) => e.stopPropagation()}
          />
        ))}
      </span>
    </div>
  );
}
