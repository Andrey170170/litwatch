/** The Topics tab: cards grouped Focus, Area, Field in the fixed order (DESIGN.md, "Topics"). */
import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import type { Schemas } from "@/api/client";
import { topicsQuery } from "@/api/queries";
import { Badge } from "@/components/Badge";
import { Fill } from "@/components/slots";
import { useDesktop } from "@/hooks/useDesktop";
import { DEPTH_NAME, RARITIES, RARITY_NAME, rc } from "@/lib/rarity";

/** The topic's papers by rarity as a thin bar, the five levels as segments sized by count. */
function Thin({ counts }: { counts: Schemas["RarityCounts"] }) {
  return (
    <div className="rthin" aria-hidden>
      {RARITIES.map((r) => (
        <span key={r} style={{ "--rc": rc(r), flex: Math.max(counts[r], 0.6) } as React.CSSProperties} title={`${RARITY_NAME[r]}: ${counts[r]}`} />
      ))}
    </div>
  );
}

function Card({ t }: { t: Schemas["TopicCard"] }) {
  return (
    <Link to="/topics/$slug" params={{ slug: t.slug }} className="tcard">
      <div className="hd">
        <Badge topic={t} size="lg" />
        <div>
          <div className="n" title={t.name}>
            {t.name}
          </div>
          <div className="s">
            {t.papers} papers · {t.saved} saved
          </div>
        </div>
      </div>
      <Thin counts={t.counts} />
      {t.newest_must_read && (
        <div className="nm2">
          <span className="k2">Newest must read</span>
          <Link to="/paper/$" params={{ _splat: t.newest_must_read.id }} search={{ topic: t.slug }} className="t2" title={t.newest_must_read.title} onClick={(e) => e.stopPropagation()}>
            {t.newest_must_read.title}
          </Link>
        </div>
      )}
    </Link>
  );
}

export function TopicsPage() {
  const { data, isPending } = useQuery(topicsQuery);
  const desktop = useDesktop();
  return (
    <>
      <Fill name="bar">
        <span className="t">Topics</span>
      </Fill>
      {desktop && <div className="h1 mb-3">Topics</div>}
      {data?.map((g) =>
        g.topics.length ? (
          <section key={g.depth}>
            <div className="sec mb-1.5 mt-1.5">{DEPTH_NAME[g.depth]}</div>
            <div className="tgrid">
              {g.topics.map((t) => (
                <Card key={t.slug} t={t} />
              ))}
            </div>
          </section>
        ) : null,
      )}
      {!isPending && !data?.some((g) => g.topics.length) && <div className="empty">No topics are followed yet.</div>}
    </>
  );
}
