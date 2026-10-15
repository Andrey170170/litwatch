/** Papers: the whole collection as shelves by mixed rarity, with search over the collection and the archive. */
import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { useNavigate, useSearch } from "@tanstack/react-router";
import { useProcess } from "@/api/mutations";
import { outsideQuery, papersQuery, topicsQuery } from "@/api/queries";
import { ListControls } from "@/components/ListControls";
import { Shelves } from "@/components/Shelves";
import { Fill } from "@/components/slots";
import { useDesktop } from "@/hooks/useDesktop";
import { rowDate } from "@/lib/format";
import { PAPER_SHELVES, RARITIES, shelvesFor, type Rarity, type Shelf } from "@/lib/rarity";
import type { ListSearch } from "@/lib/search";

function Outside({ q, search }: { q: string; search: ListSearch }) {
  const { data, isPending } = useQuery(outsideQuery(q, search));
  const process = useProcess();
  const navigate = useNavigate();
  const hits = data?.outside ?? [];
  return (
    <>
      <div className="outh">
        Outside the collection · from the archive <span className="cnt">{data ? hits.length : ""}</span>
        {data?.left_out?.length ? <span className="cnt">· {data.left_out.join(", ")} did not answer</span> : null}
      </div>
      {isPending && <div className="loadmore">Searching…</div>}
      {data && !hits.length && <div className="empty">No archive hits for “{q}”.</div>}
      {hits.map((h) => (
        <div key={h.id} className="orow">
          <span className="ti">
            {h.title}
            <br />
            <span className="au">
              {(h.authors ?? []).slice(0, 2).join(", ")}
              {(h.authors?.length ?? 0) > 2 ? ` +${h.authors!.length - 2}` : ""} · {rowDate(h.published)} · <span className="mono">{h.id}</span>
            </span>
          </span>
          <button
            className="btn"
            disabled={process.isPending}
            onClick={() => process.mutate(h.id, { onSuccess: () => void navigate({ to: "/paper/$", params: { _splat: h.id } }) })}
          >
            Process
          </button>
        </div>
      ))}
    </>
  );
}

export function PapersPage() {
  const search = useSearch({ from: "/papers" });
  const navigate = useNavigate({ from: "/papers" });
  const desktop = useDesktop();
  const { data: groups } = useQuery(topicsQuery);
  const shelves = shelvesFor(PAPER_SHELVES, search.rarity ?? []);
  const first = useInfiniteQuery(papersQuery(search, shelves[0]!, 50));
  const page0 = first.data?.pages[0];
  const query = (shelf: Shelf, limit: number) => papersQuery(search, shelf, limit);
  const onChange = (next: ListSearch) => void navigate({ search: next, replace: true });
  return (
    <>
      <Fill name="bar">
        <span className="t">Papers</span>
      </Fill>
      {desktop && <div className="h1 mb-3">Papers</div>}
      <ListControls search={search} onChange={onChange} counts={page0?.counts} total={page0 ? sumVisible(page0.counts, search) : undefined} topics={groups?.flatMap((g) => g.topics)} />
      <Shelves
        shelves={shelves}
        query={query}
        listKey={`papers:${JSON.stringify(search)}`}
        empty={page0 && sumVisible(page0.counts, search) === 0 ? <div className="empty">Nothing matches.</div> : null}
      />
      {page0 && page0.counts.unrated > 0 && !search.rarity?.length && <div className="loadmore">{page0.counts.unrated} not yet classified</div>}
      {search.q && <Outside q={search.q} search={search} />}
    </>
  );
}

/** How many papers the filters leave: the level counts under the other filters, narrowed to the chosen levels. */
function sumVisible(counts: Record<Rarity, number>, search: ListSearch): number {
  const levels: readonly Rarity[] = search.rarity?.length ? search.rarity : RARITIES;
  return levels.reduce((n, r) => n + counts[r], 0);
}
