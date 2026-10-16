/** The Topics tab: cards grouped Focus, Area, Field in the fixed order (DESIGN.md, "Topics"). */
import { useQuery } from "@tanstack/react-query";
import { Link, useNavigate } from "@tanstack/react-router";
import type { Schemas } from "@/api/client";
import { topicsQuery } from "@/api/queries";
import { Badge } from "@/components/Badge";
import { Fill } from "@/components/slots";
import { Strip } from "@/components/Strip";
import { useDesktop } from "@/hooks/useDesktop";
import { DEPTH_NAME, RARITIES, RARITY_NAME, rc } from "@/lib/rarity";

/** The topic's papers by rarity as a thin bar, the five levels as segments sized by count; one grey line when
 * it has none. */
function Thin({ counts }: { counts: Schemas["RarityCounts"] }) {
  const any = RARITIES.some((r) => counts[r] > 0);
  return (
    <div className="rthin" aria-hidden>
      {any ? RARITIES.map((r) => <span key={r} style={{ "--rc": rc(r), flex: Math.max(counts[r], 0.6) } as React.CSSProperties} title={`${RARITY_NAME[r]}: ${counts[r]}`} />) : <span style={{ "--rc": "var(--line)", flex: 1 } as React.CSSProperties} />}
    </div>
  );
}

/** The whole card opens the topic; the newest must read, a link of its own, opens that paper. */
function Card({ t }: { t: Schemas["TopicCard"] }) {
  const navigate = useNavigate();
  return (
    <div className="tcard" onClick={() => void navigate({ to: "/topics/$slug", params: { slug: t.slug } })}>
      <div className="hd">
        <Badge topic={t} size="lg" />
        <div>
          <Link to="/topics/$slug" params={{ slug: t.slug }} className="n" title={t.name} onClick={(e) => e.stopPropagation()}>
            {t.name}
          </Link>
          <div className="s">
            {t.papers} papers · {t.saved} saved
          </div>
        </div>
      </div>
      <Thin counts={t.counts} />
      <Strip strip={t.strip} depth={t.depth} small />
      {t.newest_must_read && (
        <div className="nm2">
          <span className="k2">Newest must read</span>
          <Link to="/paper/$" params={{ _splat: t.newest_must_read.id }} search={{ topic: t.slug }} className="t2" title={t.newest_must_read.title} onClick={(e) => e.stopPropagation()}>
            {t.newest_must_read.title}
          </Link>
        </div>
      )}
    </div>
  );
}

export function TopicsPage() {
  const { data, isError, refetch } = useQuery(topicsQuery);
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
      {isError && (
        <div className="loadmore">
          Could not load the topics ·{" "}
          <button className="inl" onClick={() => void refetch()}>
            Try again
          </button>
        </div>
      )}
      {data && !data.some((g) => g.topics.length) && <div className="empty">No topics are followed yet.</div>}
    </>
  );
}
