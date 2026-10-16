/**
 * Topics and the topic editor (DESIGN.md, "Topics and the topic editor", "The re-run offer"): the lists in the
 * fixed order, a row dragged by its grip within its depth, the editor with Save that does nothing until something
 * changed, and after a save the offer in place of Save and Cancel.
 */
import { useQuery } from "@tanstack/react-query";
import { Link, useNavigate, useParams } from "@tanstack/react-router";
import { useEffect, useRef, useState, type PointerEvent as ReactPointerEvent } from "react";
import type { Schemas } from "@/api/client";
import { usePendingOrder, useReorderTopics, useSaveTopic } from "@/api/mutations";
import { keywordsQuery, settingsTaxonomyQuery, taxonomyStatsQuery } from "@/api/queries";
import { Badge } from "@/components/Badge";
import { Seg } from "@/components/Seg";
import { DEPTH_NAME, type Depth } from "@/lib/rarity";
import { savedLine } from "@/lib/settings";
import { Failed, Field, Later, Load, PageHead } from "./parts";

type Topic = Schemas["Topic"];
type Tree = Schemas["Taxonomy"];
type Class = Schemas["TaxonomyClass"];

const DEPTHS: Depth[] = ["focus", "area", "field"];
const STATUSES: Schemas["Status"][] = ["active", "paused"];
/** A topic loaded as a draft (imported or created through the API) shows so; it is activated by an explicit choice. */
const DRAFT_STATUSES: Schemas["Status"][] = ["draft", "active", "paused"];
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
  // The order while a row is dragged; the topics' own otherwise.
  const [drag, setDrag] = useState<{ slug: string; order: string[] } | null>(null);
  const order = drag?.order ?? topics.map((t) => t.class);
  const by = Object.fromEntries(topics.map((t) => [t.class, t]));

  const start = (slug: string) => (e: ReactPointerEvent<HTMLButtonElement>) => {
    e.currentTarget.setPointerCapture(e.pointerId);
    setDrag({ slug, order });
  };
  const move = (e: ReactPointerEvent<HTMLButtonElement>) => {
    const over = document.elementFromPoint(e.clientX, e.clientY)?.closest<HTMLElement>("[data-slug]")?.dataset.slug;
    if (over && by[over]) setDrag((d) => d && { ...d, order: moved(d.order, d.slug, over) });
  };
  const end = () => {
    if (!drag) return;
    setDrag(null);
    if (drag.order.join() !== topics.map((t) => t.class).join()) onOrder(depth, drag.order);
  };
  if (!topics.length) return null;
  return (
    <>
      <div className="cap">{DEPTH_NAME[depth]}</div>
      <div className="list">
        {order.map((slug) => {
          const t = by[slug]!;
          const path = classPath(tree, slug);
          const name = path.at(-1) ?? slug;
          return (
            <div className={`trow${drag?.slug === slug ? " drag" : ""}`} key={slug} data-slug={slug}>
              <button className="grip" aria-label={`Move ${name}`} title="Drag to reorder" onPointerDown={start(slug)} onPointerMove={move} onPointerUp={end} onPointerCancel={end}>
                <i />
              </button>
              <Link to="/settings/topics/$slug" params={{ slug }} className="nm">
                <Badge topic={{ name, depth: t.depth, monogram: t.monogram, colour: t.colour }} />
                <span className="txt">
                  {name}
                  {t.status !== "active" && <span className="faint"> · {t.status}</span>}
                  <span className="g">{path.join(" › ")}</span>
                </span>
                {revisions[slug] != null && <span className="mono faint">v{revisions[slug]}</span>}
              </Link>
            </div>
          );
        })}
      </div>
    </>
  );
}

/** `topics` in `order` (slugs first to last; one not named keeps to the end), for a move still being saved. */
export function inOrder<T extends { class: string }>(topics: T[], order: string[] | undefined): T[] {
  if (!order) return topics;
  const at = new Map(order.map((s, i) => [s, i]));
  return [...topics].sort((a, b) => (at.get(a.class) ?? order.length) - (at.get(b.class) ?? order.length));
}

export function TopicsPage() {
  const tree = useQuery(settingsTaxonomyQuery("topics"));
  const stats = useQuery(taxonomyStatsQuery("topics"));
  const reorder = useReorderTopics();
  const t = tree.data;
  // A move being saved shows at once and stays shown over any read until its save has refreshed the tree.
  const topics = inOrder(t?.topics ?? [], usePendingOrder());
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
      <Load q={tree} />
      <Failed q={stats} what="the topics' revisions" />
      {t && DEPTHS.map((d) => <DepthList key={d} depth={d} topics={byDepth(d)} tree={t} revisions={stats.data?.topic_revisions ?? {}} onOrder={onOrder} />)}
      {t && !topics.length && <div className="empty">No topic is followed yet. Pick a class in the Taxonomy to follow it.</div>}
    </>
  );
}

// -- the editor ----------------------------------------------------------------------------------------------------

/** A topic as the form holds it: the optional texts as strings, so the form compares as it edits. */
function asForm(t: Topic): Topic {
  return { ...t, rubric: t.rubric ?? "", interests: t.interests ?? "", keywords: t.keywords ?? [] };
}

/** A new topic's defaults when a class is first followed: its description as the subject, initials as monogram. */
function fresh(cls: Class): Topic {
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
  const tree = useQuery(settingsTaxonomyQuery(`topics/${slug}`));
  const cls = tree.data?.classes.find((c) => c.slug === slug);
  const current = tree.data?.topics?.find((t) => t.class === slug) ?? null;
  // The editor shows on the cached tree while a fresh visit refetches it ("Updating…" above).
  return (
    <>
      <Load q={tree} />
      {tree.data && (cls ? <TopicEditor key={slug} tree={tree.data} cls={cls} current={current} /> : <p className="empty">There is no such class.</p>)}
    </>
  );
}

function TopicEditor({ tree, cls, current }: { tree: Tree; cls: Class; current: Topic | null }) {
  const stats = useQuery(taxonomyStatsQuery(`topics/${cls.slug}`));
  const save = useSaveTopic();
  const navigate = useNavigate();
  // The form against what was loaded, or what was last saved here; the taxonomy's refetch after a save does
  // not reopen it.
  const [base, setBase] = useState(() => asForm(current ?? fresh(cls)));
  const [form, setForm] = useState(base);
  // What the last save here sent and replaced: the offer reads these, not the form, which may be edited again.
  const [saved, setSaved] = useState<{ before: Topic | null; after: Topic } | null>(null);
  // The keyword being typed, or null while the "+ keyword" button shows.
  const [typing, setTyping] = useState<string | null>(null);
  const matches = useQuery(keywordsQuery(cls.slug, !!current));
  const path = classPath(tree, cls.slug);
  const dirty = JSON.stringify(form) !== JSON.stringify(base);
  // What the API would refuse (a subject under 20 characters, no monogram, a Focus topic without a rubric) is not
  // offered to save; a class not followed yet can be followed with its generated defaults once they pass.
  const valid =
    form.subject.trim().length >= 20 &&
    form.monogram.trim().length > 0 &&
    (form.depth !== "focus" || (form.rubric ?? "").trim().length > 0);
  const savable = valid && (dirty || !current);
  // The taxonomy refreshed behind a pristine form (edited elsewhere meanwhile): the form follows it, so a save sends
  // the current fields, not the ones loaded before. An edited form keeps its edits.
  const loaded = JSON.stringify(asForm(current ?? fresh(cls)));
  useEffect(() => {
    if (!dirty && !saved && loaded !== JSON.stringify(base)) {
      const next = JSON.parse(loaded) as Topic;
      setBase(next);
      setForm(next);
    }
  }, [loaded, dirty, saved, base]);
  // The topic's version once the stats arrive; until then the head leaves it out and a save says "a new version".
  const version = stats.data?.topic_revisions[cls.slug];
  // An edit after a save brings Save back in the offer's place.
  const set = (patch: Partial<Topic>) => {
    setForm((f) => ({ ...f, ...patch }));
    setSaved(null);
  };
  const keywords = form.keywords ?? [];
  const addKeyword = () => {
    const k = typing?.trim();
    if (k && !keywords.includes(k)) set({ keywords: [...keywords, k] });
    setTyping(null);
  };
  // The form as it is now, for a save that returns after further edits: the offer describes only what was sent.
  const latest = useRef(form);
  latest.current = form;
  const onSave = () => {
    const body: Topic = { ...form, rubric: form.depth === "focus" ? form.rubric || null : null, interests: form.interests || null };
    save.mutate(
      { slug: cls.slug, topic: body },
      {
        onSuccess: () => {
          setBase(form);
          if (latest.current === form) setSaved({ before: current, after: form });
        },
      },
    );
  };
  return (
    <div className="setpg">
      <PageHead title={cls.name} sub={current ? `${version == null ? "" : `Version ${version} · `}follows ${path.join(" › ")}` : `Not followed yet · ${path.join(" › ")}`} deeper />
      <Failed q={stats} what="the topic's version" />
      <div className="form">
        <Field label="Class">
          <div className="tin one faint">{path.join(" › ")}</div>
        </Field>
        <Field label="Depth" hint="Focus is graded by two labs, Area checked, Field graded once">
          <Seg values={DEPTHS} value={form.depth} onPick={(depth) => depth && set({ depth })} label="Depth" />
        </Field>
        <Field label="Subject" hint="what the topic is about; the graders read it; at least 20 characters">
          <textarea className="tin" rows={3} value={form.subject} onChange={(e) => set({ subject: e.target.value })} aria-label="Subject" />
        </Field>
        {form.depth === "focus" && (
          <Field label="Rubric" hint="central, related, adjacent: what each means here; a Focus topic needs one">
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
            {typing != null ? (
              <input
                className="kw"
                autoFocus
                value={typing}
                aria-label="New keyword"
                placeholder="a regex, matched case-insensitively"
                onChange={(e) => setTyping(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") addKeyword();
                  if (e.key === "Escape") setTyping(null);
                }}
                onBlur={addKeyword}
              />
            ) : (
              <button className="kw add" onClick={() => setTyping("")}>
                + keyword
              </button>
            )}
          </div>
          <Failed q={matches} what="the keyword matches" />
        </Field>
        <Field label="Look">
          <div className="look">
            <Badge topic={{ name: cls.name, depth: form.depth, monogram: form.monogram || "?", colour: form.colour }} size="lg" />
            <input className="tin sm mono" value={form.monogram} maxLength={2} onChange={(e) => set({ monogram: e.target.value })} aria-label="Monogram" />
            <span className="sws" role="group" aria-label="Colour">
              {SWATCH.map((c) => (
                <button key={c} style={{ background: c }} className={form.colour === c ? "on" : ""} aria-label={c} aria-pressed={form.colour === c} onClick={() => set({ colour: c })} />
              ))}
            </span>
          </div>
        </Field>
        <Field label="Status" hint="a paused topic keeps its papers and gets no new ones">
          <Seg values={base.status === "draft" ? DRAFT_STATUSES : STATUSES} value={form.status} onPick={(status) => status && set({ status })} label="Status" />
        </Field>
      </div>
      <div className="mt-4">
        {saved ? (
          <div className="offer">
            <div className="oh">{savedLine(saved.before, saved.after, version)}</div>
            <div className="nsub">Re-run it now on:</div>
            {["Labelled papers and reported misses", "The topic's existing members", "Papers matching its keywords · last 90 days", "A date range"].map((row) => (
              <div className="ofr off" key={row}>
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
            <button className="btn pri" disabled={!savable || save.isPending} title={savable ? undefined : !valid ? "The subject needs at least 20 characters, the badge a monogram, and a Focus topic a rubric" : "Nothing changed"} onClick={onSave}>
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
