/** Paper rows (DESIGN.md, anatomy): the reading row (title, Gist, meta) and the one-line table row. */
import { Link } from "@tanstack/react-router";
import type { MouseEvent } from "react";
import type { Schemas } from "@/api/client";
import { useSave } from "@/api/mutations";
import { rowDate } from "@/lib/format";
import { openedFrom } from "@/lib/listContext";
import { CONTRIBUTION_NAME, rc, type Rarity } from "@/lib/rarity";
import { Badge } from "./Badge";

export type Row = Schemas["Row"];

/** ★ saves and unsaves in place. */
export function Star({ id, saved, className = "sv" }: { id: string; saved: boolean; className?: string }) {
  const save = useSave(id);
  const onClick = (e: MouseEvent) => {
    e.preventDefault();
    e.stopPropagation();
    save.mutate(!saved);
  };
  return (
    <button className={`${className}${saved ? " on" : ""} hit`} onClick={onClick} disabled={save.isPending} title={saved ? "Saved · unsave" : "Save"} aria-pressed={saved} aria-label={saved ? "Unsave" : "Save"}>
      {saved ? "★" : "☆"}
    </button>
  );
}

export function Facets({ facets }: { facets: Schemas["Facets"] | null | undefined }) {
  if (!facets) return <span className="fx" />;
  return (
    <span className="fx">
      {facets.releases_code && <i title="Code released">⟨/⟩</i>}
      {facets.releases_weights && <i title="Weights released">◆</i>}
    </span>
  );
}

type RowProps = {
  row: Row;
  /** The rarity colouring the row: the list's own. */
  rarity: Rarity | null | undefined;
  /** The topic the list belongs to, carried to the paper's page; none on a mixed list, which shows badges. */
  topic?: string;
  /** The list this row is in, in order, so the paper's page knows its place. */
  list: { key: string; ids: string[]; total: number };
};

function linkProps(row: Row, topic: string | undefined, list: RowProps["list"]) {
  return {
    to: "/paper/$" as const,
    params: { _splat: row.id },
    search: topic ? { topic } : {},
    onClick: () => openedFrom(list.key, list.ids, list.total),
  };
}

export function ReadingRow({ row, rarity, topic, list }: RowProps) {
  return (
    <Link {...linkProps(row, topic, list)} className="row br" style={{ "--rc": rc(rarity) } as React.CSSProperties} data-state={row.state ?? undefined}>
      <div className="l1">
        <span className="ti">{row.title}</span>
        <Star id={row.id} saved={row.saved} />
      </div>
      {row.gist && <div className="g">{row.gist}</div>}
      <div className="m">
        {!topic && (row.badges?.length ?? 0) > 0 && (
          <span className="bs">
            {row.badges?.map((b) => (
              <Badge key={b.slug} topic={b} size="sm" />
            ))}
          </span>
        )}
        {row.facets && <span className="kd">{CONTRIBUTION_NAME[row.facets.contribution]}</span>}
        <Facets facets={row.facets} />
        <span className="sp" />
        <span className="dt">{rowDate(row.published)}</span>
      </div>
    </Link>
  );
}

export function TableRow({ row, rarity, topic, list }: RowProps) {
  return (
    <Link {...linkProps(row, topic, list)} className={`row tr${topic ? "" : " wb"}`} style={{ "--rc": rc(rarity) } as React.CSSProperties} data-state={row.state ?? undefined} title={row.gist ?? undefined}>
      <span className="ti">{row.title}</span>
      {!topic && (
        <span className="bs">
          {row.badges?.map((b) => (
            <Badge key={b.slug} topic={b} size="sm" />
          ))}
        </span>
      )}
      <span className="kd">{row.facets ? CONTRIBUTION_NAME[row.facets.contribution] : ""}</span>
      <Facets facets={row.facets} />
      <span className="dt">{rowDate(row.published)}</span>
      <Star id={row.id} saved={row.saved} />
    </Link>
  );
}
