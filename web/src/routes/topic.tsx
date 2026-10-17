/** A topic's page: its head, the controls, and its papers as shelves by grade (DESIGN.md, "A topic page"). */
import { useInfiniteQuery } from "@tanstack/react-query";
import { useNavigate, useParams, useSearch } from "@tanstack/react-router";
import { ApiError } from "@/api/client";
import { topicQuery } from "@/api/queries";
import { Badge } from "@/components/Badge";
import { ListControls } from "@/components/ListControls";
import { Back } from "@/components/Shell";
import { Shelves } from "@/components/Shelves";
import { Fill } from "@/components/slots";
import { Strip } from "@/components/Strip";
import { CADENCE, TOPIC_SHELVES, type Shelf } from "@/lib/rarity";
import { visibleCount, type ListSearch } from "@/lib/search";

export function TopicPage() {
  const { slug } = useParams({ from: "/topics/$slug" });
  const search = useSearch({ from: "/topics/$slug" });
  const navigate = useNavigate({ from: "/topics/$slug" });
  const first = useInfiniteQuery(topicQuery(slug, search, TOPIC_SHELVES[0]!, 50));
  const page0 = first.data?.pages[0];
  const topic = page0?.topic;
  const query = (shelf: Shelf, limit: number) => topicQuery(slug, search, shelf, limit);
  const onChange = (next: ListSearch) => void navigate({ search: next, replace: true });
  // A failed later page or refetch keeps the page and its rows (the shelf offers the retry); only a topic that
  // never loaded is reported here.
  if (first.isError && !page0)
    return (
      <>
        <Back />
        {first.error instanceof ApiError && first.error.status === 404 ? (
          <div className="empty">No such topic.</div>
        ) : (
          <div className="loadmore">
            Could not load the topic ·{" "}
            <button className="inl" onClick={() => void first.refetch()}>
              Try again
            </button>
          </div>
        )}
      </>
    );
  const total = page0 ? visibleCount(page0.counts, search) : undefined;
  return (
    <>
      <Fill name="title">
        {topic && <Badge topic={topic} />}
        <span className="t">{topic?.name ?? ""}</span>
      </Fill>
      <Back />
      {topic && (
        <>
          <div className="thead">
            <Badge topic={topic} size="lg" />
            <div>
              <div className="dep">
                {topic.depth} · {CADENCE[topic.depth]}
              </div>
              <div className="h1">{topic.name}</div>
            </div>
          </div>
          <p className="tsub">{topic.subject}</p>
          <Strip strip={topic.strip} depth={topic.depth} />
        </>
      )}
      <ListControls search={search} onChange={onChange} counts={page0?.counts} total={total} topic={topic ? { name: topic.name } : { name: "the topic" }} />
      <Shelves shelves={TOPIC_SHELVES} query={query} topic={slug} listKey={`topic:${slug}:${JSON.stringify(search)}`} empty={total === 0 ? <div className="empty">{search.q || search.rarity || search.saved || search.code ? "Nothing matches." : "Nothing in this topic yet."}</div> : null} />
    </>
  );
}
