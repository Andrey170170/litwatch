/**
 * Topics and the topic editor (DESIGN.md, "Topics and the topic editor", "The re-run offer"): the lists in the
 * fixed order, a row dragged by its grip within its depth, the editor with Save that does nothing until something
 * changed, and after a save the offer in place of Save and Cancel.
 */
import { useQuery } from "@tanstack/react-query";
import { Link, useNavigate, useParams } from "@tanstack/react-router";
import { useEffect, useMemo, useRef, useState, type PointerEvent as ReactPointerEvent } from "react";
import type { Schemas } from "@/api/client";
import { useReorderTopics, useSaveTopic } from "@/api/mutations";
import { keywordsQuery, taxonomyQuery, taxonomyStatsQuery } from "@/api/queries";
import { Badge } from "@/components/Badge";
import { DEPTH_NAME, type Depth } from "@/lib/rarity";
import { savedLine } from "@/lib/settings";
import { Later, Load, PageHead } from "./parts";

type Topic = Schemas["Topic"];
type Tree = Schemas["Taxonomy"];
type Status = Schemas["Status"];

const DEPTHS: Depth[] = ["focus", "area", "field"];
export const SWATCH = ["#c026d3", "#e11d48", "#ea580c", "#ca8a04", "#65a30d", "#0d9488", "#0891b2", "#0284c7", "#4f46e5"];

/** "Interpretability › Chain-of-thought faithfulness": the class and its ancestors, top first. */
export function classPath(tree: Tree, slug: string): string[] {
  const by = Object.fromEntries(tree.classes.map((c) => [c.slug, c]));
  const path: string[] = [];
  for (let c = by[slug]; c; c = c.parent ? by[c.parent] : undefined) path.unshift(c.name);
  return path;
}

/** Moves `slug` within `order` to the place of `over`. */
export function moved(order: string[], slug: string, over: string): string[] {
  const from = order.indexOf(slug);
  const to = order.indexOf(over);
  if (from < 0 || to < 0 || from === to) return order;
  const next = order.filter((s) => s !== slug);
  next.splice(to, 0, slug);
  return next;
}

/** One depth's rows; a row drags by its grip to a new place in the list. The order is sent once the drag ends. */
function DepthList({ depth, topics, tree, revisions, onOrder }: { depth: Depth; topics: Topic[]; tree: Tree; revisions: Record<string, number>; onOrder: (depth: Depth, slugs: string[]) => void }) {
  const [order, setOrder] = useState(() => topics.map((t) => t.class));
  const [dragging, setDragging] = useState<string | null>(null);
  const list = useRef<HTMLDivElement>(null);
  useEffect(() => setOrder(topics.map((t) => t.class)), [topics]);
  const by = Object.fromEntries(topics.map((t) => [t.class, t]));
  const name = (t: Topic) => tree.classes.find((c) => c.slug === t.class)?.name ?? t.class;

  const start = (slug: string) => (e: ReactPointerEvent<HTMLButtonElement>) => {
    e.currentTarget.setPointerCapture(e.pointerId);
    setDragging(slug);
  };
  const move = (e: ReactPointerEvent<HTMLButtonElement>) => {
    if (!dragging) return;
    const over = document.elementFromPoint(e.clientX, e.clientY)?.closest<HTMLElement>("[data-slug]")?.dataset.slug;
    if (over && list.current?.querySelector(`[data-slug="${over}"]`)) setOrder((o) => moved(o, dragging, over));
  };
  const end = () => {
    if (!dragging) return;
    setDragging(null);
    if (order.join() !== topics.map((t) => t.class).join()) onOrder(depth, order);
  };
  if (!topics.length) return null;
  return (
    <>
      <div className="cap">{DEPTH_NAME[depth]}</div>
      <div className="list" ref={list} data-depth={depth}>
        {order.map((slug) => {
          const t = by[slug];
          if (!t) return null;
          const path = classPath(tree, slug);
          return (
            <div className={`trow${dragging === slug ? " drag" : ""}`} key={slug} data-slug={slug}>
              <button className="grip" aria-label={`Move ${name(t)}`} title="Drag to reorder" onPointerDown={start(slug)} onPointerMove={move} onPointerUp={end} onPointerCancel={end}>
                <i />
              </button>
              <Link to="/settings/topics/$slug" params={{ slug }} className="nm">
                <Badge topic={{ name: name(t), depth: t.depth, monogram: t.monogram, colour: t.colour }} />
                <span className="txt">
                  {name(t)}
                  {t.status !== "active" && <span className="faint"> · {t.status}</span>}
                  <span className="g">{path.join(" › ")}</span>
                </span>
                <span className="mono faint">v{revisions[slug] ?? 1}</span>
              </Link>
            </div>
          );
        })}
      </div>
    </>
  );
}

export function TopicsPage() {
  const tree = useQuery(taxonomyQuery);
  const stats = useQuery(taxonomyStatsQuery);
  const reorder = useReorderTopics();
  const t = tree.data;
  const topics = t?.topics ?? [];
  const byDepth = (d: Depth) => topics.filter((t) => t.depth === d);
  // The new order of one depth, spliced into the whole: the API takes every topic once.
  const onOrder = (depth: Depth, slugs: string[]) => reorder.mutate(DEPTHS.flatMap((d) => (d === depth ? slugs : byDepth(d).map((t) => t.class))));
  return (
    <>
      <PageHead title="Topics" sub="Each follows one class of the taxonomy. The order here is the order of the sidebar and the Digest; drag a row by its grip to move it.">
        <Link to="/settings/$page" params={{ page: "taxonomy" }} className="btn">
          New topic
        </Link>
      </PageHead>
      <Load error={tree.isError} retry={() => void tree.refetch()} loading={tree.isPending} />
      {t && DEPTHS.map((d) => <DepthList key={d} depth={d} topics={byDepth(d)} tree={t} revisions={stats.data?.topic_revisions ?? {}} onOrder={onOrder} />)}
      {t && !topics.length && <div className="empty">No topic is followed yet. Pick a class in the Taxonomy to follow it.</div>}
    </>
  );
}

// -- the editor ----------------------------------------------------------------------------------------------------

function Field({ label, hint, children }: { label: string; hint?: string; children: React.ReactNode }) {
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

function Seg<T extends string>({ values, value, onPick, label, names }: { values: T[]; value: T; onPick: (v: T) => void; label: string; names?: Record<T, string> }) {
  return (
    <div className="fitseg" role="group" aria-label={label}>
      {values.map((v) => (
        <button key={v} className={`seg${value === v ? " on" : ""}`} aria-pressed={value === v} onClick={() => onPick(v)}>
          {names?.[v] ?? v}
        </button>
      ))}
    </div>
  );
}

/** A new topic's defaults when a class is first followed: its description as the subject, initials as monogram. */
function fresh(cls: Schemas["TaxonomyClass"]): Topic {
  const initials = cls.name
    .split(/[\s-]+/)
    .filter((w) => /^[A-Za-z]/.test(w))
    .slice(0, 2)
    .map((w) => w[0]!.toUpperCase())
    .join("");
  return { class: cls.slug, depth: "focus", monogram: initials || cls.name.slice(0, 2).toUpperCase(), colour: SWATCH[0]!, status: "active", subject: cls.description, rubric: "", interests: "", keywords: [] };
}

export function TopicEditorPage() {
  const { slug } = useParams({ from: "/settings/topics/$slug" });
  const tree = useQuery(taxonomyQuery);
  const stats = useQuery(taxonomyStatsQuery);
  const save = useSaveTopic();
  const navigate = useNavigate();
  const cls = tree.data?.classes.find((c) => c.slug === slug);
  const current = useMemo(() => tree.data?.topics?.find((t) => t.class === slug) ?? null, [tree.data, slug]);
  const [form, setForm] = useState<Topic | null>(null);
  const [saved, setSaved] = useState<{ before: Topic | null } | null>(null);
  const [newKeyword, setNewKeyword] = useState("");
  const [adding, setAdding] = useState(false);
  const matches = useQuery(keywordsQuery(slug, !!current));
  // The form opens on the topic as loaded, and again after a save shows a fresh one.
  useEffect(() => {
    if (cls && !form) setForm(current ?? fresh(cls));
  }, [cls, current, form]);
  if (tree.isPending || !form) return <Load error={tree.isError} retry={() => void tree.refetch()} loading />;
  if (!cls) return <p className="empty">There is no such class.</p>;
  const name = cls.name;
  const path = classPath(tree.data!, slug);
  const base = current ?? fresh(cls);
  const dirty = JSON.stringify({ ...form, rubric: form.rubric ?? "", interests: form.interests ?? "" }) !== JSON.stringify({ ...base, rubric: base.rubric ?? "", interests: base.interests ?? "" });
  const version = stats.data?.topic_revisions[slug] ?? 1;
  const set = (patch: Partial<Topic>) => setForm((f) => (f ? { ...f, ...patch } : f));
  const keywords = form.keywords ?? [];
  const addKeyword = () => {
    const k = newKeyword.trim();
    if (k && !keywords.includes(k)) set({ keywords: [...keywords, k] });
    setNewKeyword("");
    setAdding(false);
  };
  const onSave = () => {
    const body: Topic = { ...form, rubric: form.depth === "focus" ? form.rubric || null : null, interests: form.interests || null };
    save.mutate({ slug, topic: body }, { onSuccess: () => setSaved({ before: current }) });
  };
  return (
    <div className="setpg">
      <PageHead title={name} sub={current ? `Version ${version} · follows ${path.join(" › ")}` : `Not followed yet · ${path.join(" › ")}`} deeper />
      <div className="form">
        <Field label="Class">
          <div className="tin one faint">{path.join(" › ")}</div>
        </Field>
        <Field label="Depth" hint="Focus is graded by two labs, Area checked, Field graded once">
          <Seg values={DEPTHS} value={form.depth} onPick={(depth) => set({ depth })} label="Depth" />
        </Field>
        <Field label="Subject" hint="what the topic is about; the graders read it">
          <textarea className="tin" rows={3} value={form.subject} onChange={(e) => set({ subject: e.target.value })} aria-label="Subject" />
        </Field>
        {form.depth === "focus" && (
          <Field label="Rubric" hint="central, related, adjacent: what each means here">
            <textarea className="tin" rows={4} value={form.rubric ?? ""} onChange={(e) => set({ rubric: e.target.value })} aria-label="Rubric" />
          </Field>
        )}
        <Field label="Interests" hint="what makes a paper matter more">
          <textarea className="tin" rows={2} value={form.interests ?? ""} onChange={(e) => set({ interests: e.target.value })} aria-label="Interests" />
        </Field>
        <Field label="Keywords" hint="matches in the last 90 days">
          <div className="kws">
            {keywords.map((k) => (
              <span className="kw" key={k}>
                {k}
                {matches.data && k in matches.data && <span className="mono faint">{matches.data[k]}</span>}
                <button className="x" aria-label={`Remove ${k}`} onClick={() => set({ keywords: keywords.filter((x) => x !== k) })}>
                  ×
                </button>
              </span>
            ))}
            {adding ? (
              <input
                className="kw"
                autoFocus
                value={newKeyword}
                aria-label="New keyword"
                placeholder="a regex, matched case-insensitively"
                onChange={(e) => setNewKeyword(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") addKeyword();
                  if (e.key === "Escape") setAdding(false);
                }}
                onBlur={addKeyword}
              />
            ) : (
              <button className="kw add" onClick={() => setAdding(true)}>
                + keyword
              </button>
            )}
          </div>
        </Field>
        <Field label="Look">
          <div className="look">
            <Badge topic={{ name, depth: form.depth, monogram: form.monogram || "?", colour: form.colour }} size="lg" />
            <input className="tin sm mono" value={form.monogram} maxLength={3} onChange={(e) => set({ monogram: e.target.value })} aria-label="Monogram" />
            <span className="sws" role="group" aria-label="Colour">
              {SWATCH.map((c) => (
                <button key={c} style={{ background: c }} className={form.colour === c ? "on" : ""} aria-label={c} aria-pressed={form.colour === c} onClick={() => set({ colour: c })} />
              ))}
            </span>
          </div>
        </Field>
        <Field label="Status" hint="a paused topic keeps its papers and gets no new ones">
          <Seg values={["active", "paused"] as Status[]} value={form.status === "draft" ? "active" : form.status} onPick={(status) => set({ status })} label="Status" />
        </Field>
      </div>
      <div className="mt-4">
        {saved ? (
          <div className="offer">
            <div className="oh">{savedLine(saved.before, form, version)}</div>
            <div className="nsub">Re-run it now on:</div>
            {["Labelled papers and reported misses", "The topic's existing members", "Papers matching its keywords · last 90 days", "A date range"].map((row) => (
              <div className="orow off" key={row}>
                <span className="box" />
                <span className="ol">{row}</span>
                <span className="mono faint">–</span>
              </div>
            ))}
            <Later>The scoped re-runs come with the jobs API; nothing runs by itself. The edit applies forward meanwhile.</Later>
            <div className="nact">
              <Link to="/settings/$page" params={{ page: "topics" }} className="btn pri">
                Back to Topics
              </Link>
              <button className="btn" onClick={() => setSaved(null)}>
                Keep editing
              </button>
            </div>
          </div>
        ) : (
          <div className="nact">
            <button className="btn pri" disabled={!dirty || save.isPending} title={dirty ? undefined : "Nothing changed"} onClick={onSave}>
              {current ? "Save" : "Follow"}
            </button>
            <button className="btn" onClick={() => void navigate({ to: "/settings/$page", params: { page: "topics" } })}>
              Cancel
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
