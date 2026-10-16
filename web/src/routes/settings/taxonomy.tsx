/** Taxonomy (DESIGN.md, "Taxonomy"): the tree as rows with a caret that folds a branch, the badge of the topic
 * following a class, members and threshold; a row opens the class in place. */
import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { useState } from "react";
import type { Schemas } from "@/api/client";
import { settingsTaxonomyQuery, taxonomyStatsQuery } from "@/api/queries";
import { Badge } from "@/components/Badge";
import { Failed, Field, Later, Load, PageHead } from "./parts";

type Class = Schemas["TaxonomyClass"];
type Tree = Schemas["Taxonomy"];

export type Row = { cls: Class; depth: number; children: boolean };

/** The classes in tree order, each with its depth and whether it has children; parents before children, siblings
 * as the tree lists them. A folded branch shows its head alone. */
export function rows(tree: Tree, folded = new Set<string>()): Row[] {
  const kids = new Map<string | null, Class[]>();
  for (const c of tree.classes) kids.set(c.parent ?? null, [...(kids.get(c.parent ?? null) ?? []), c]);
  const out: Row[] = [];
  const walk = (parent: string | null, depth: number) => {
    for (const c of kids.get(parent) ?? []) {
      out.push({ cls: c, depth, children: kids.has(c.slug) });
      if (!folded.has(c.slug)) walk(c.slug, depth + 1);
    }
  };
  walk(null, 0);
  return out;
}

/** Folded at first: a top-level branch no followed topic lives in. */
export function foldedAtFirst(tree: Tree): Set<string> {
  const followed = new Set((tree.topics ?? []).map((t) => t.class));
  const by = Object.fromEntries(tree.classes.map((c) => [c.slug, c]));
  const roots = new Set<string>();
  for (const slug of followed) for (let c = by[slug]; c; c = c.parent ? by[c.parent] : undefined) if (!c.parent) roots.add(c.slug);
  return new Set(tree.classes.filter((c) => !c.parent && !roots.has(c.slug)).map((c) => c.slug));
}

export function TaxonomyPage() {
  const tree = useQuery(settingsTaxonomyQuery("taxonomy"));
  const stats = useQuery(taxonomyStatsQuery("taxonomy"));
  const [folded, setFolded] = useState<Set<string> | null>(null);
  const [open, setOpen] = useState<string | null>(null);
  const t = tree.data;
  const fold = folded ?? (t ? foldedAtFirst(t) : new Set<string>());
  const toggle = (slug: string) => setFolded((f) => {
    const next = new Set(f ?? fold);
    if (next.has(slug)) next.delete(slug);
    else next.add(slug);
    return next;
  });
  const following = Object.fromEntries((t?.topics ?? []).map((x) => [x.class, x]));
  return (
    <>
      <PageHead title="Taxonomy" sub="Every paper is classified into this tree. A topic follows one class; edits apply forward.">
        <a className="btn" href="/api/settings/taxonomy.yaml" download="taxonomy.yaml">
          Export YAML
        </a>
        <button className="btn" disabled title="Import comes with the taxonomy write API; until then, litwatch taxonomy import on the command line">
          Import YAML
        </button>
      </PageHead>
      <Load q={tree} />
      <Failed q={stats} what="the members and thresholds" />
      {t && (
        <div className="list ctree">
          <div className="crow hd">
            <span className="car" />
            <span className="nm">Class</span>
            <span className="mem">Members</span>
            <span className="th">Threshold</span>
          </div>
          {rows(t, fold).map((r) => {
            const shut = r.children && fold.has(r.cls.slug);
            const topic = following[r.cls.slug];
            const on = open === r.cls.slug;
            // The threshold the class is admitted at; blank until the stats arrive.
            const th = stats.data ? (stats.data.thresholds[r.cls.slug] ?? 0).toFixed(2) : "";
            return (
              <div key={r.cls.slug}>
                <div className={`crow d${Math.min(r.depth, 2)}${on ? " on" : ""}`}>
                  {r.children ? (
                    <button className={`car${shut ? "" : " open"}`} aria-label={shut ? `Unfold ${r.cls.name}` : `Fold ${r.cls.name}`} aria-expanded={!shut} onClick={() => toggle(r.cls.slug)} />
                  ) : (
                    <span className="car" />
                  )}
                  <button className="nm" onClick={() => setOpen(on ? null : r.cls.slug)} aria-expanded={on}>
                    {r.cls.name}
                  </button>
                  {topic && <Badge topic={{ name: r.cls.name, depth: topic.depth, monogram: topic.monogram, colour: topic.colour }} size="sm" />}
                  <span className="mono faint mem">{stats.data ? (stats.data.members[r.cls.slug] ?? 0).toLocaleString("en") : ""}</span>
                  <span className={`mono th${r.cls.threshold == null ? " faint" : ""}`}>{th}</span>
                </div>
                {on && (
                  <div className="clsed">
                    <div className="form">
                      <Field label="Name">
                        <div className="tin one">{r.cls.name}</div>
                      </Field>
                      <Field label="Description" hint="says what is out as well as what is in">
                        <div className="tin">{r.cls.description}</div>
                      </Field>
                      <Field label="Threshold" hint="a paper is a member at or above it">
                        <div className="tin one sm mono">
                          {th}
                          {r.cls.threshold == null && <span className="faint"> · the default for its depth</span>}
                        </div>
                      </Field>
                    </div>
                    <div className="nact">
                      <Link to="/settings/topics/$slug" params={{ slug: r.cls.slug }} className="btn pri">
                        {topic ? "Edit the topic" : "Follow this class"}
                      </Link>
                      <button className="btn" onClick={() => setOpen(null)}>
                        Close
                      </button>
                    </div>
                    <Later>Editing a class (name, description, threshold) comes with the taxonomy write API; the YAML import changes it meanwhile.</Later>
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}
    </>
  );
}
