import { describe, expect, it, vi } from "vitest";
import { PAPER_SHELVES, TOPIC_SHELVES } from "@/lib/rarity";
import { freshClient } from "@/test/harness";
import * as q from "./queries";

/** Every read the app makes, each with a key of its own. */
const search = { q: "sparse autoencoders" };
const reads: [string, { queryKey: readonly unknown[]; initialPageParam?: unknown }][] = [
  ["summary", q.summaryQuery],
  ["topics", q.topicsQuery],
  ["paper", q.paperQuery("2610.00001")],
  ["search scope", q.searchScopeQuery],
  ["taxonomy", q.taxonomyQuery],
  ["a Papers shelf", q.papersQuery(search, PAPER_SHELVES[0]!)],
  ["the archive", q.outsideQuery(search.q, search)],
  ["a topic shelf", q.topicQuery("mi", search, TOPIC_SHELVES[0]!)],
  ["day", q.dayQuery("2026-11-10")],
  ["week", q.weekQuery("2026-11-10")],
  ["digest", q.digestQuery(7)],
  ["history", q.historyQuery("2026-11-10", 14)],
  ["quality", q.qualityQuery("needs")],
  ["misses", q.missesQuery],
  ["sessions", q.sessionsQuery("calibrate")],
  ["session", q.sessionQuery(3)],
  ["runs", q.runsQuery("runs")],
  ["schedule", q.scheduleQuery],
  ["jobs", q.jobsQuery("runs")],
  ["rerun offer", q.rerunOfferQuery("mi", ["keywords"], null, null)],
  ["replay offer", q.replayOfferQuery],
  ["config", q.configQuery],
  ["integrations", q.integrationsQuery("integrations")],
  ["settings taxonomy", q.settingsTaxonomyQuery("taxonomy")],
  ["taxonomy stats", q.taxonomyStatsQuery("taxonomy")],
  ["keywords", q.keywordsQuery("mi")],
];

describe("a cancelled read", () => {
  it.each(reads)("stops its request: %s", async (_name, options) => {
    // A request that never answers, and tells whether it was aborted.
    const seen: AbortSignal[] = [];
    vi.stubGlobal("fetch", (req: Request) => {
      seen.push(req.signal);
      return new Promise<Response>((_, reject) => req.signal.addEventListener("abort", () => reject(req.signal.reason)));
    });
    const client = freshClient();
    const read = "initialPageParam" in options ? client.fetchInfiniteQuery(options as never) : client.fetchQuery(options as never);
    await vi.waitFor(() => expect(seen).toHaveLength(1));
    await client.cancelQueries({ queryKey: options.queryKey });
    await read.catch(() => {});
    expect(seen[0]!.aborted).toBe(true);
  });
});
