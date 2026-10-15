/**
 * A list page's controls (DESIGN.md, "Topics" and "Papers"): search, the topic badges (Papers), Saved and Code,
 * the rarity bar, the count and the sort. On the desktop in rows above the shelves; on the phone the search box and
 * a filter button in the bottom bar, the rest in a sheet.
 */
import { useEffect, useState } from "react";
import type { Schemas } from "@/api/client";
import { useDesktop } from "@/hooks/useDesktop";
import { RARITIES, RARITY_NAME, rc, segmentFlex, type Rarity } from "@/lib/rarity";
import { activeFilters, sortOf, toggled, type ListSearch, type Sort } from "@/lib/search";
import { Badge } from "./Badge";
import { Sheet } from "./Sheet";
import { Fill } from "./slots";

type Counts = Schemas["RarityCounts"];
type Props = {
  search: ListSearch;
  onChange: (next: ListSearch) => void;
  counts: Counts | undefined;
  total: number | undefined;
  /** The topic this list belongs to; Papers has none and shows the topic badges as filters. */
  topic?: { name: string };
  topics?: Schemas["Badge"][];
};

/** The rarity bar: five segments sized by count, each a filter; several can be on and the rest dim. */
export function RarityBar({ counts, on, onToggle, inline }: { counts: Counts; on: Rarity[]; onToggle: (r: Rarity) => void; inline?: boolean }) {
  const flex = segmentFlex(counts);
  return (
    <div className={`rbar${inline ? " inl" : ""}`} role="group" aria-label="Rarity">
      {RARITIES.map((r) => (
        <button
          key={r}
          className={`rseg${on.includes(r) ? " on" : ""}${on.length && !on.includes(r) ? " off" : ""}`}
          style={{ "--rc": rc(r), flex: flex[r] } as React.CSSProperties}
          onClick={() => onToggle(r)}
          aria-label={`${RARITY_NAME[r]}: ${counts[r]}`}
          aria-pressed={on.includes(r)}
        >
          <b>{counts[r]}</b>
          <span>{RARITY_NAME[r]}</span>
        </button>
      ))}
    </div>
  );
}

function SearchBox({ value, onChange, placeholder, className }: { value: string; onChange: (q: string) => void; placeholder: string; className?: string }) {
  const [text, setText] = useState(value);
  useEffect(() => setText(value), [value]);
  useEffect(() => {
    if (text === value) return;
    const t = window.setTimeout(() => onChange(text), 250);
    return () => window.clearTimeout(t);
  }, [text, value, onChange]);
  return (
    <label className={`qbox${className ? ` ${className}` : ""}`}>
      ⌕
      <input value={text} onChange={(e) => setText(e.target.value)} placeholder={placeholder} aria-label={placeholder} type="search" />
    </label>
  );
}

function SortSeg({ search, onChange }: { search: ListSearch; onChange: (s: Sort) => void }) {
  const on = sortOf(search);
  const sorts: [Sort, string][] = [["newest", "Newest"], ["rarest", "Rarest"], ...(search.q ? ([["best", "Best match"]] as [Sort, string][]) : [])];
  return (
    <span className="sortseg" role="group" aria-label="Sort">
      {sorts.map(([k, n]) => (
        <button key={k} className={on === k ? "on" : ""} onClick={() => onChange(k)} aria-pressed={on === k}>
          {n}
        </button>
      ))}
    </span>
  );
}

function TopicToggles({ topics, on, onToggle }: { topics: Schemas["Badge"][]; on: string[]; onToggle: (slug: string) => void }) {
  return (
    <div className="tfs" role="group" aria-label="Topics">
      {topics.map((t) => (
        <button key={t.slug} className={`tf${on.includes(t.slug) ? " on" : ""}${on.length && !on.includes(t.slug) ? " off" : ""}`} onClick={() => onToggle(t.slug)} title={t.name} aria-pressed={on.includes(t.slug)}>
          <Badge topic={t} />
        </button>
      ))}
    </div>
  );
}

export function ListControls({ search, onChange, counts, total, topic, topics }: Props) {
  const desktop = useDesktop();
  const [sheet, setSheet] = useState(false);
  const set = (patch: Partial<ListSearch>) => onChange({ ...search, ...patch });
  const q = search.q ?? "";
  const placeholder = topic ? `Search in ${topic.name}` : desktop ? "Search the collection and the archive" : "Search papers";
  const chips = (
    <>
      <button className={`fchip${search.saved ? " on" : ""}`} onClick={() => set({ saved: search.saved ? undefined : true })} aria-pressed={!!search.saved}>
        {topic || !desktop ? "★ Saved" : "★ Saved · the library"}
      </button>
      <button className={`fchip${search.code ? " on" : ""}`} onClick={() => set({ code: search.code ? undefined : true })} aria-pressed={!!search.code}>
        ⟨/⟩ Code
      </button>
    </>
  );
  const bar = (inline: boolean) => <RarityBar counts={counts ?? { legendary: 0, epic: 0, rare: 0, uncommon: 0, common: 0, unrated: 0 }} on={search.rarity ?? []} onToggle={(r) => set({ rarity: toggled(search.rarity, r) })} inline={inline} />;
  const count = <span className="cnt">{total === undefined ? "" : `${total} papers`}</span>;
  const sort = <SortSeg search={search} onChange={(s) => set({ sort: s })} />;
  const topicToggles = topics && !topic ? <TopicToggles topics={topics} on={search.topic ?? []} onToggle={(slug) => set({ topic: toggled(search.topic, slug) })} /> : null;

  if (desktop)
    return (
      <>
        {!topic && (
          <>
            <div className="ctlrow">
              <SearchBox value={q} onChange={(v) => set({ q: v || undefined })} placeholder={placeholder} className="big" />
            </div>
            {topicToggles && <div className="ctlrow">{topicToggles}</div>}
          </>
        )}
        <div className="ctlrow">
          {topic && <SearchBox value={q} onChange={(v) => set({ q: v || undefined })} placeholder={placeholder} className="short" />}
          {chips}
          {bar(true)}
          {count}
          {sort}
        </div>
      </>
    );

  const n = activeFilters(search, !topic);
  return (
    <Fill name="foot">
      <div className="pfoot qbar">
        <SearchBox value={q} onChange={(v) => set({ q: v || undefined })} placeholder={placeholder} />
        <button className="fbtn" onClick={() => setSheet(true)} aria-label="Filters">
          ⧩{n > 0 && <i>{n}</i>}
        </button>
      </div>
      <Sheet open={sheet} onClose={() => setSheet(false)} label="Filters">
        {topicToggles && (
          <>
            <div className="cap">Topics</div>
            {topicToggles}
          </>
        )}
        <div className="cap">Rarity</div>
        {bar(false)}
        <div className="cap">Show</div>
        <div className="srow">{chips}</div>
        <div className="cap">Sort</div>
        <div className="srow">
          {sort}
          <span style={{ flex: 1 }} />
          {count}
        </div>
        <button className="done" onClick={() => setSheet(false)}>
          Done
        </button>
      </Sheet>
    </Fill>
  );
}
