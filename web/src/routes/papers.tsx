import { useMemo, useState } from "react";
import { useNavigate, useSearch } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import { topicsQuery } from "@/api/queries";
import { Chip, PapersTable, SearchBox, SortSelect, YearChip, type TableFilter } from "@/components/PapersTable";
import { ShareResult } from "@/components/ShareResult";
import { FilterSheet } from "@/routes/topics";
import { useDesktop } from "@/hooks/useDesktop";

/** The whole collection as one table, with the miss flow under it (DESIGN.md "Papers"). */
export function PapersPage() {
  const desktop = useDesktop();
  const search = useSearch({ from: "/papers" });
  const navigate = useNavigate({ from: "/papers" });
  const topics = useQuery(topicsQuery);
  const watch = useMemo(() => topics.data?.filter((t) => t.kind === "watch") ?? [], [topics.data]);
  const onFilter = (patch: Partial<TableFilter & { lookup: string }>) =>
    void navigate({ search: (prev) => ({ ...prev, page: undefined, ...patch }) });

  const filters = (
    <>
      <select
        value={search.topic ?? ""}
        onChange={(e) => onFilter({ topic: e.target.value || undefined, adjacent: undefined })}
        aria-label="Topic"
        className={`min-h-[36px] rounded-full border border-border bg-transparent px-3 text-[13px] text-muted-foreground ${search.topic ? "border-foreground bg-foreground text-background" : ""}`}
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
      {!desktop && <SortSelect sort={search.sort} hasQuery={!!search.q} onChange={(sort) => onFilter({ sort })} />}
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
        showMiss={!!search.q}
        surfaceTopic={null}
        above={search.lookup ? <ShareResult q={search.lookup} topics={topics.data ?? []} /> : null}
      />
      <LookupPrompt onLookup={(q) => onFilter({ lookup: q })} initial={search.lookup} />
    </div>
  );
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
