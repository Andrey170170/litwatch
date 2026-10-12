import { useState, type ReactNode } from "react";
import { Link } from "@tanstack/react-router";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { useQuery } from "@tanstack/react-query";
import { Check, ExternalLink, FileText } from "lucide-react";
import type { Row, TopicCard } from "@/api/client";
import { bibtexFor, bibtexForFilter, download, useMiss } from "@/api/mutations";
import { papersQuery } from "@/api/queries";
import { GradeDot } from "@/components/paper-bits";
import { useDesktop } from "@/hooks/useDesktop";
import { authorsLine, fmt } from "@/lib/format";
import type { Sort, TableSearch } from "@/lib/search";
import { cn } from "@/lib/utils";

export const PAGE_SIZE = 50;

export type TableFilter = TableSearch & { topic?: string };

type Props = {
  filter: TableFilter;
  onFilter: (patch: Partial<TableFilter>) => void;
  /** The Watch topics, for the miss report. */
  watchTopics: TopicCard[];
  /** Set on a topic page: no dot words beyond the grade, no topic in the row. */
  fixedTopic?: TopicCard;
  /** Offer "Should have been found for a topic…" on rows that no topic shows. */
  showMiss?: boolean;
  /** Rows to show in their own band above the table (the lookup result). */
  above?: ReactNode;
};

/** The one table: a topic page with the topic fixed, or the whole collection (DESIGN.md "Row"). */
export function PapersTable({ filter, onFilter, watchTopics, fixedTopic, showMiss, above }: Props) {
  const desktop = useDesktop();
  const page = filter.page ?? 0;
  const q = useQuery(papersQuery({ ...filter, limit: PAGE_SIZE, offset: page * PAGE_SIZE }));
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const rows = q.data?.rows ?? [];
  const total = q.data?.total ?? 0;
  const picks = fixedTopic ? fixedTopic.kind !== "watch" : false;

  function toggle(id: string) {
    setSelected((s) => {
      const n = new Set(s);
      if (n.has(id)) n.delete(id);
      else n.add(id);
      return n;
    });
  }

  async function exportBibtex() {
    const text = selected.size ? await bibtexFor([...selected]) : await bibtexForFilter(filter);
    download(text, `litwatch${fixedTopic ? `-${fixedTopic.slug}` : ""}.bib`);
  }

  const sort: Sort = filter.sort ?? (filter.q ? "published" : "added");
  const head = (key: Sort, label: string, className?: string) => (
    <th className={cn("py-2 pr-2.5 text-left text-[12.5px] font-medium whitespace-nowrap", className)}>
      <button
        type="button"
        onClick={() => onFilter({ sort: key, page: undefined })}
        className={cn("min-h-[32px] text-muted-foreground hover:text-foreground", sort === key && "text-foreground")}
        aria-sort={sort === key ? "descending" : "none"}
      >
        {label}
        {sort === key ? " ↓" : ""}
      </button>
    </th>
  );

  return (
    <div>
      <div className="mt-2 flex items-center justify-between gap-3 text-[13px] text-muted-foreground">
        <span className="tabular">
          {q.isPending ? "…" : total === 0 ? "No papers match." : `${page * PAGE_SIZE + 1}–${Math.min(total, (page + 1) * PAGE_SIZE)} of ${total}`}
          {selected.size > 0 && ` · ${selected.size} selected`}
        </span>
        <button type="button" onClick={() => void exportBibtex()} className="min-h-[36px] underline decoration-border hover:text-foreground" disabled={total === 0 && !selected.size}>
          {selected.size ? `BibTeX for ${selected.size} selected` : "Export BibTeX"}
        </button>
      </div>
      {above}
      {desktop ? (
        <table className="mt-1 w-full border-collapse text-[13.5px]">
          <thead>
            <tr className="border-b border-foreground">
              <th className="w-8" />
              <th className="w-5" />
              {head("title", "Title")}
              {head("published", "Published")}
              {head("added", "Added")}
              <th className="py-2 pr-2.5 text-left text-[12.5px] font-medium text-muted-foreground">Take</th>
              <th />
              <th />
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <DesktopRow
                key={r.id}
                row={r}
                picks={picks}
                selected={selected.has(r.id)}
                onSelect={() => toggle(r.id)}
                miss={showMiss && !shownRow(r) ? <MissLink paperId={r.id} topics={watchTopics} /> : null}
              />
            ))}
          </tbody>
        </table>
      ) : (
        <div className="mt-1">
          {rows.map((r) => (
            <PhoneRow
              key={r.id}
              row={r}
              picks={picks}
              selected={selected.has(r.id)}
              onSelect={() => toggle(r.id)}
              miss={showMiss && !shownRow(r) ? <MissLink paperId={r.id} topics={watchTopics} /> : null}
            />
          ))}
        </div>
      )}
      {total > PAGE_SIZE && (
        <div className="mt-2.5 flex justify-between text-[13px] text-muted-foreground">
          <button type="button" disabled={page === 0} onClick={() => onFilter({ page: page - 1 || undefined })} className="min-h-[36px] underline decoration-border disabled:no-underline disabled:opacity-40">
            newer
          </button>
          <button type="button" disabled={(page + 1) * PAGE_SIZE >= total} onClick={() => onFilter({ page: page + 1 })} className="min-h-[36px] underline decoration-border disabled:no-underline disabled:opacity-40">
            older
          </button>
        </div>
      )}
    </div>
  );
}

function shownRow(r: Row) {
  return r.grade === "must_read" || r.grade === "relevant";
}

function Select({ checked, onChange, title }: { checked: boolean; onChange: () => void; title: string }) {
  return (
    <label className="hit cursor-pointer" aria-label={`Select ${title}`}>
      <input type="checkbox" className="peer sr-only" checked={checked} onChange={onChange} />
      <span className={cn("grid size-4 place-items-center rounded-[4px] border border-border peer-focus-visible:outline-2", checked && "border-foreground bg-foreground text-background")}>
        {checked && <Check className="size-3" strokeWidth={3} aria-hidden />}
      </span>
    </label>
  );
}

function Links({ id }: { id: string }) {
  return (
    <span className="inline-flex items-center gap-0.5 text-muted-foreground">
      <a href={`https://arxiv.org/abs/${id}`} target="_blank" rel="noreferrer" className="hit" aria-label="arXiv">
        <ExternalLink className="size-4" strokeWidth={1.75} aria-hidden />
      </a>
      <a href={`https://arxiv.org/pdf/${id}`} target="_blank" rel="noreferrer" className="hit" aria-label="PDF">
        <FileText className="size-4" strokeWidth={1.75} aria-hidden />
      </a>
    </span>
  );
}

type RowProps = { row: Row; picks: boolean; selected: boolean; onSelect: () => void; miss: ReactNode };

function DesktopRow({ row: r, picks, selected, onSelect, miss }: RowProps) {
  const adjacent = r.grade === "adjacent";
  return (
    <tr className={cn("border-b border-border align-top", adjacent && "text-muted-foreground")}>
      <td className="py-1.5">
        <Select checked={selected} onChange={onSelect} title={r.title} />
      </td>
      <td className="py-[15px] pr-1">{!picks && <GradeDot grade={r.grade} grading={r.grading} />}</td>
      <td className="min-w-[220px] py-[11px] pr-2.5 font-serif text-[16.5px] leading-[1.3] font-semibold">
        <Link to="/paper/$" params={{ _splat: r.id }} className="hover:underline decoration-border">
          {r.title}
        </Link>
        <small className="meta mt-0.5 block text-[12.5px] font-normal">{adjacent ? "near the topic" : authorsLine(r.authors, 5)}</small>
      </td>
      <td className="tabular py-[11px] pr-2.5 whitespace-nowrap text-muted-foreground">{fmt.cell(r.published)}</td>
      <td className="tabular py-[11px] pr-2.5 whitespace-nowrap text-muted-foreground">{fmt.cell(r.added)}</td>
      <td className="max-w-[360px] py-[11px] pr-2.5 text-body">
        {r.take && (
          <span className="line-clamp-3" title={r.take}>
            {r.take}
          </span>
        )}
        {r.also_in && r.also_in.length > 0 && <span className="meta"> also in {r.also_in.join(", ")}</span>}
        {miss && <div>{miss}</div>}
      </td>
      <td className="py-[11px] pr-2.5 text-[12.5px] font-semibold whitespace-nowrap text-success">{r.saved ? "Saved" : ""}</td>
      <td className="py-1.5 whitespace-nowrap">
        <Links id={r.id} />
      </td>
    </tr>
  );
}

/** The same row on the phone: title, authors, take, one small line of dates, the saved mark. */
function PhoneRow({ row: r, picks, selected, onSelect, miss }: RowProps) {
  const adjacent = r.grade === "adjacent";
  return (
    <div className={cn("grid grid-cols-[14px_1fr_auto] gap-x-2.5 border-t border-border py-[11px]", adjacent && "text-muted-foreground")}>
      <div className="pt-[7px]">{!picks && <GradeDot grade={r.grade} grading={r.grading} />}</div>
      <div>
        <h3 className="font-serif text-[15.5px] leading-[1.3] font-semibold">
          <Link to="/paper/$" params={{ _splat: r.id }}>
            {r.title}
          </Link>
        </h3>
        <p className="meta mt-0.5">{adjacent ? "near the topic" : authorsLine(r.authors)}</p>
        {r.take && <p className="mt-1 line-clamp-3 text-[13.5px] text-body">{r.take}</p>}
        {r.also_in && r.also_in.length > 0 && <p className="meta">also in {r.also_in.join(", ")}</p>}
        {miss}
        <p className="meta tabular mt-1 flex flex-wrap items-center gap-x-3 text-[12.5px]">
          {r.published && <span>{fmt.cell(r.published)}</span>}
          {r.added && <span>added {fmt.cell(r.added)}</span>}
          {r.saved && <span className="font-semibold text-success">Saved</span>}
          <Links id={r.id} />
        </p>
      </div>
      <div className="-mt-2 -mr-2.5">
        <Select checked={selected} onChange={onSelect} title={r.title} />
      </div>
    </div>
  );
}

/** The amber link that starts a miss report: it asks which Watch topic, then files it. */
export function MissLink({ paperId, topics }: { paperId: string; topics: TopicCard[] }) {
  const miss = useMiss();
  const [open, setOpen] = useState(false);
  if (topics.length === 0) return null;
  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <button type="button" className="min-h-[36px] text-[13px] font-medium text-warning hover:underline">
          Should have been found for a topic…
        </button>
      </PopoverTrigger>
      <PopoverContent align="start" className="w-auto max-w-[320px] rounded-xl p-2 text-[13px]">
        <p className="meta px-1 pb-1">Which topic should have had it?</p>
        <div className="flex flex-wrap gap-1.5">
          {topics.map((t) => (
            <button
              key={t.slug}
              type="button"
              disabled={miss.isPending}
              className="min-h-[36px] rounded-full border border-border px-3 hover:bg-muted"
              onClick={() => {
                miss.mutate({ paper_id: paperId, topic: t.slug });
                setOpen(false);
              }}
            >
              {t.name}
            </button>
          ))}
        </div>
      </PopoverContent>
    </Popover>
  );
}

/** The filter chips: a row on a desktop, inside a sheet on the phone. */
export function Chip({ on, onClick, children }: { on?: boolean; onClick: () => void; children: ReactNode }) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={on}
      className={cn(
        "min-h-[36px] rounded-full border border-border px-3 text-[13px] text-muted-foreground hover:text-foreground",
        on && "border-foreground bg-foreground text-background hover:text-background",
      )}
    >
      {children}
    </button>
  );
}

export function YearChip({ year, onChange }: { year: number | undefined; onChange: (y: number | undefined) => void }) {
  const now = new Date().getFullYear();
  const years = Array.from({ length: 12 }, (_, i) => now - i);
  return (
    <select
      value={year ?? ""}
      onChange={(e) => onChange(e.target.value ? Number(e.target.value) : undefined)}
      aria-label="Year"
      className={cn(
        "min-h-[36px] rounded-full border border-border bg-transparent px-3 text-[13px] text-muted-foreground",
        year && "border-foreground bg-foreground text-background",
      )}
    >
      <option value="">Any year</option>
      {years.map((y) => (
        <option key={y} value={y}>
          {y}
        </option>
      ))}
    </select>
  );
}

export function SearchBox({ value, onChange, placeholder }: { value: string | undefined; onChange: (q: string | undefined) => void; placeholder: string }) {
  const [draft, setDraft] = useState(value ?? "");
  return (
    <form
      className="flex min-w-[160px] flex-1 items-center rounded-full border border-border px-3"
      onSubmit={(e) => {
        e.preventDefault();
        onChange(draft.trim() || undefined);
      }}
    >
      <input
        type="search"
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={() => draft.trim() !== (value ?? "") && onChange(draft.trim() || undefined)}
        placeholder={placeholder}
        aria-label={placeholder}
        className="min-h-[36px] w-full bg-transparent text-[13.5px] outline-none placeholder:text-muted-foreground"
      />
    </form>
  );
}
