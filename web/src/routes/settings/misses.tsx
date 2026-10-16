/** Misses (DESIGN.md, "Misses"): each report with its topic, date, the stage that let it go, and the trace. */
import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import type { Schemas } from "@/api/client";
import { missesQuery } from "@/api/queries";
import { Badge } from "@/components/Badge";
import { rowDate } from "@/lib/format";
import { admits } from "@/lib/rarity";
import { Later, Load, PageHead, PaperLink, useTopicCards } from "./parts";

type Miss = Schemas["Miss"];
type Cell = { name: string; value: string; tone: "ok" | "bad" | "off" };

/** The five cells of a trace: what each stage concluded for the topic. Cyan where the stage would have admitted
 * the paper, red where it let it go, grey while the stage has not run on this text. The classifier's cell reads
 * its probability against the threshold; a paper admitted through a class under the topic says so. */
export function cells(t: Miss["trace"]): Cell[] {
  const pending: Cell["tone"] = "off";
  const p = t.probability;
  const read = p == null ? "no answer" : `${p.toFixed(2)} ${p >= t.threshold ? "over" : "under"} ${t.threshold.toFixed(2)}`;
  const under = t.below ? " · in a class under it" : "";
  return [
    { name: "Classifier", value: !t.classified ? "pending" : read + under, tone: !t.classified ? pending : t.classifier ? "ok" : "bad" },
    { name: "Uncertain band", value: !t.classified ? "pending" : t.uncertain ? "member" : t.classifier ? "–" : "under", tone: !t.classified ? pending : t.uncertain ? "ok" : t.classifier ? "off" : "bad" },
    { name: "Keywords", value: !t.classified ? "pending" : t.keyword ? "match" : "no match", tone: !t.classified ? pending : t.keyword ? "ok" : "bad" },
    { name: "Judge", value: !t.graded ? "pending" : (t.judge ?? "–"), tone: !t.graded ? pending : admits(t.judge) ? "ok" : "bad" },
    { name: "Check", value: !t.graded ? "pending" : (t.check ?? "not asked"), tone: !t.graded || !t.check ? pending : admits(t.check) ? "ok" : "bad" },
  ];
}

/** Where the pipeline let the paper go: the first stage whose cells all read red. */
export function missedAt(t: Miss["trace"]): string {
  if (!t.classified) return "not classified yet";
  if (!t.classifier && !t.uncertain && !t.keyword) return "classification";
  if (!t.graded) return "not graded yet";
  if (!admits(t.judge) && !(t.check && admits(t.check))) return "grading";
  return "nowhere: the trace admits it";
}

export function MissesPage() {
  const misses = useQuery(missesQuery);
  const cards = useTopicCards();
  return (
    <>
      <PageHead title="Misses" sub="Papers reported as missed, with what each stage said. A trace shows where the pipeline let the paper go." />
      <Load q={misses} />
      {misses.data?.map((m) => {
        const t = cards[m.topic];
        const at = missedAt(m.trace);
        return (
          <div className="miss" key={m.id}>
            <div className="nh">
              {t && <Badge topic={t} />}
              <PaperLink id={m.paper_id} topic={m.topic} className="nt">
                {m.title}
              </PaperLink>
              <span className="dt ml-auto">{rowDate(m.reported_at)}</span>
            </div>
            <div className="nb">
              {t?.name ?? m.topic} · missed at <b className="badt">{at}</b>
            </div>
            <div className="trace">
              {cells(m.trace).map((c) => (
                <div className={`stg ${c.tone}`} key={c.name}>
                  <span>{c.name}</span>
                  <b>{c.value}</b>
                </div>
              ))}
            </div>
            <div className="nact">
              <Link to="/settings/topics/$slug" params={{ slug: m.topic }} className="btn">
                Edit the topic
              </Link>
              <Link to="/settings/$page" params={{ page: "taxonomy" }} className="btn">
                Edit the class
              </Link>
            </div>
          </div>
        );
      })}
      {misses.data && !misses.data.length && <div className="empty">No miss has been reported.</div>}
      {misses.data && <Later>"Resolved" waits on a write to the miss record.</Later>}
    </>
  );
}
