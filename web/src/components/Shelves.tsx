/**
 * Shelves: a list cut by rarity (Papers) or grade (a topic page), the top ones open with reading rows, the lower
 * ones folded to a count until opened; each loads more as it scrolls (DESIGN.md, "Topics").
 */
import { useInfiniteQuery, type InfiniteData, type QueryKey, type UseInfiniteQueryOptions } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { rc, type Shelf } from "@/lib/rarity";
import { ReadingRow, TableRow, type Row } from "./Rows";

type Page = { rows: Row[]; total: number };
/** The options for one shelf's query: a page of rows and the shelf's total, whatever else the response carries. */
export type ShelfQuery<P extends Page = Page, K extends QueryKey = QueryKey> = (shelf: Shelf, limit: number) => UseInfiniteQueryOptions<P, Error, InfiniteData<P>, K, number>;

function Sentinel({ onSeen }: { onSeen: () => void }) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const el = ref.current;
    if (!el || typeof IntersectionObserver === "undefined") return;
    const io = new IntersectionObserver((entries) => entries.some((e) => e.isIntersecting) && onSeen(), { rootMargin: "300px" });
    io.observe(el);
    return () => io.disconnect();
  }, [onSeen]);
  return (
    <div ref={ref} className="loadmore">
      Loading more…
    </div>
  );
}

function ShelfView<P extends Page, K extends QueryKey>({ shelf, query, topic, listKey }: { shelf: Shelf; query: ShelfQuery<P, K>; topic?: string; listKey: string }) {
  const [open, setOpen] = useState(shelf.open);
  const q = useInfiniteQuery(query(shelf, open ? 50 : 1));
  const total = q.data?.pages[0]?.total;
  if (total === 0 || (total === undefined && !q.isPending)) return null;
  const rows = open ? (q.data?.pages.flatMap((p) => p.rows) ?? []) : [];
  const style = { "--rc": rc(shelf.colour) } as React.CSSProperties;
  const list = { key: `${listKey}:${shelf.key}`, ids: rows.map((r) => r.id) };
  const Head = shelf.open ? "div" : "button";
  return (
    <section aria-label={shelf.name}>
      <Head className={`shh${open ? "" : " fold"}`} style={style} onClick={shelf.open ? undefined : () => setOpen((o) => !o)}>
        <span className="sq" />
        {shelf.name}
        <span className="k">{total ?? ""}</span>
        {!shelf.open && <span className="tg">{open ? "fold" : "show"}</span>}
      </Head>
      {open && (
        <div className={shelf.form === "table" ? "tbl" : "blist"}>
          {rows.map((r) =>
            shelf.form === "reading" ? (
              <ReadingRow key={r.id} row={r} rarity={r.rarity} topic={topic} list={list} />
            ) : (
              <TableRow key={r.id} row={r} rarity={r.rarity} topic={topic} list={list} />
            ),
          )}
          {q.hasNextPage && !q.isFetchingNextPage && <Sentinel onSeen={() => void q.fetchNextPage()} />}
          {q.isFetchingNextPage && <div className="loadmore">Loading more…</div>}
        </div>
      )}
    </section>
  );
}

export function Shelves<P extends Page, K extends QueryKey>({ shelves, query, topic, listKey, empty }: { shelves: Shelf[]; query: ShelfQuery<P, K>; topic?: string; listKey: string; empty: React.ReactNode }) {
  return (
    <>
      {shelves.map((s) => (
        <ShelfView key={s.key} shelf={s} query={query} topic={topic} listKey={listKey} />
      ))}
      {empty}
    </>
  );
}
