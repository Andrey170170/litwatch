import { useMemo, useState } from "react";
import { useNavigate, useSearch } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import { api, ok } from "@/api/client";
import { topicsQuery } from "@/api/queries";
import { Chip, MissLink, PapersTable, SearchBox, YearChip, type TableFilter } from "@/components/PapersTable";
import { GradeDot } from "@/components/paper-bits";
import { FilterSheet } from "@/routes/topics";
import { useDesktop } from "@/hooks/useDesktop";
import { authorsLine, fmt } from "@/lib/format";
import type { Sort } from "@/lib/search";
import { Link } from "@tanstack/react-router";

/** The whole collection as one table, with the miss flow under it (DESIGN.md "Papers"). */
export function PapersPage() {
  const desktop = useDesktop();
  const search = useSearch({ from: "/papers" });
  const navigate = useNavigate({ from: "/papers" });
  const topics = useQuery(topicsQuery);
  const watch = useMemo(() => topics.data?.filter((t) => t.kind === "watch") ?? [], [topics.data]);
  const onFilter = (patch: Partial<TableFilter & { lookup: string }>) =>
    void navigate({ search: (prev) => ({ ...prev, page: undefined, ...patch }) });

  const sorts: { key: Sort; word: string }[] = [
    { key: "added", word: "Added" },
    { key: "published", word: "Published" },
    { key: "title", word: "Title" },
  ];
  const filters = (
    <>
      <select
        value={search.topic ?? ""}
        onChange={(e) => onFilter({ topic: e.target.value || undefined, adjacent: undefined })}
        aria-label="Topic"
        className={`min-h-[36px] appearance-none rounded-full border border-border bg-transparent px-3 text-[13px] text-muted-foreground ${search.topic ? "border-foreground bg-foreground text-background" : ""}`}
      >
        <option value="">Any topic</option>
        {(topics.data ?? []).map((t) => (
          <option key={t.slug} value={t.slug}>
            {t.name}
          </option>
        ))}
      </select>
      <Chip on={search.saved === true} onClick={() => onFilter({ saved: search.saved ? undefined : true })}>
        Saved
      </Chip>
      <YearChip year={search.year} onChange={(year) => onFilter({ year })} />
      {!desktop && (
        <select
          value={search.sort ?? (search.q ? "published" : "added")}
          onChange={(e) => onFilter({ sort: e.target.value as Sort })}
          aria-label="Sort"
          className="min-h-[36px] appearance-none rounded-full border border-border bg-transparent px-3 text-[13px] text-muted-foreground"
        >
          {sorts.map((s) => (
            <option key={s.key} value={s.key}>
              Sort by {s.word.toLowerCase()}
            </option>
          ))}
        </select>
      )}
    </>
  );

  return (
    <div className="mx-auto max-w-[1180px] px-4 pt-4 pb-8 desk:px-7 desk:pt-6">
      <div className="flex flex-wrap items-center gap-2 text-[13px]">
        <SearchBox value={search.q} onChange={(q) => onFilter({ q })} placeholder="Search titles and abstracts" />
        {desktop ? filters : <FilterSheet>{filters}</FilterSheet>}
      </div>
      {search.q && <p className="meta mt-2">Titles and abstracts, newest first.</p>}
      <PapersTable
        filter={search}
        onFilter={onFilter}
        watchTopics={watch}
        showMiss
        above={search.lookup ? <LookupBand q={search.lookup} watch={watch} /> : null}
      />
      <LookupPrompt onLookup={(q) => onFilter({ lookup: q })} initial={search.lookup} />
    </div>
  );
}

/** The looked-up paper in its own band above the table, with the miss report offered. */
function LookupBand({ q, watch }: { q: string; watch: ReturnType<typeof useLookupTopics> }) {
  const look = useQuery({
    queryKey: ["lookup", q],
    queryFn: async () => ok(await api.POST("/api/lookup", { body: { q } })),
    retry: false,
  });
  return (
    <div className="mt-3 rounded-[10px] border border-border bg-muted px-3.5 py-3">
      {look.isPending && <p className="meta">Looking up {q}…</p>}
      {look.isError && <p className="text-[13.5px] text-destructive">Nothing found for “{q}”. An arXiv id or link is what works here.</p>}
      {look.data && (
        <div className="grid grid-cols-[14px_1fr] gap-x-2.5">
          <div className="pt-[7px]">
            <GradeDot grade={look.data.row.grade} grading={look.data.row.grading} />
          </div>
          <div>
            <p className="meta text-[12px] uppercase tracking-[.05em]">{look.data.fetched ? "Fetched into the collection just now" : "Already in the collection"}</p>
            <h3 className="font-serif text-[16.5px] leading-[1.3] font-semibold">
              <Link to="/paper/$" params={{ _splat: look.data.row.id }}>
                {look.data.row.title}
              </Link>
            </h3>
            <p className="meta">
              {authorsLine(look.data.row.authors)} · {fmt.cell(look.data.row.published)}
              {look.data.row.saved && <span className="font-semibold text-success"> · Saved</span>}
            </p>
            {look.data.row.take && <p className="mt-1 text-[13.5px] text-body">{look.data.row.take}</p>}
            <MissLink paperId={look.data.row.id} topics={watch} />
          </div>
        </div>
      )}
    </div>
  );
}

function useLookupTopics() {
  return useQuery(topicsQuery).data?.filter((t) => t.kind === "watch") ?? [];
}

function LookupPrompt({ onLookup, initial }: { onLookup: (q: string) => void; initial: string | undefined }) {
  const [open, setOpen] = useState(!!initial);
  const [q, setQ] = useState("");
  return (
    <div className="mt-4 border-t border-border pt-3 text-[13px] text-muted-foreground">
      {open ? (
        <form
          className="flex flex-wrap items-center gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            if (q.trim()) onLookup(q.trim());
          }}
        >
          <input
            autoFocus
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="arXiv id or link"
            aria-label="arXiv id or link"
            className="min-h-[40px] min-w-[220px] flex-1 rounded-full border border-border bg-transparent px-3.5 text-[14px] text-foreground outline-none"
          />
          <button type="submit" className="min-h-[40px] rounded-full bg-foreground px-4 text-[13.5px] font-medium text-background">
            Look up
          </button>
        </form>
      ) : (
        <>
          Not here?{" "}
          <button type="button" onClick={() => setOpen(true)} className="min-h-[36px] underline decoration-border hover:text-foreground">
            Look up an arXiv id or link
          </button>
          , which fetches the paper and offers the same report.
        </>
      )}
    </div>
  );
}
