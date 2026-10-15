import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";
import { json, renderApp, scriptFetch, viewport } from "@/test/harness";

const counts = { legendary: 1, epic: 1, rare: 1, uncommon: 4, common: 9, unrated: 2 };
const row = (id: string, title: string, rarity: string, saved = false) => ({
  id,
  title,
  authors: ["A. Author"],
  published: "2026-10-13",
  gist: `Gist of ${title}`,
  rarity,
  badges: [{ slug: "mi", name: "Mech interp", depth: "focus", monogram: "MI", colour: "#f3e600", grade: "must_read", rarity }],
  saved,
  facets: { contribution: "method", releases_code: true, releases_weights: false },
});

afterEach(() => sessionStorage.clear());

describe("Papers", () => {
  it("shows each rarity shelf from its own page, folded shelves as counts, and saves in place", async () => {
    viewport(true);
    const calls = scriptFetch({
      "GET /api/status": { state: "quiet", papers: 212, awaiting_classification: 0, analysis_failed: 0, ungraded: 0 },
      "GET /api/topics": [{ depth: "focus", topics: [] }, { depth: "area", topics: [] }, { depth: "field", topics: [] }],
      "GET /api/papers": ({ url }) => {
        const q = new URL(url, "http://x").searchParams;
        const rarity = q.get("rarity") ?? "";
        if (rarity.includes("epic")) return { rows: [row("2610.00001", "Legendary one", "legendary"), row("2610.00002", "Epic one", "epic")], total: 2, counts };
        if (rarity === "rare") return { rows: [row("2610.00003", "Rare one", "rare")], total: 1, counts };
        if (rarity === "uncommon") return { rows: [], total: 4, counts };
        return { rows: [], total: 9, counts };
      },
      "PUT /api/library/2610.00002": { message: null, saved: true, grades: {} },
    });
    renderApp("/papers");
    const top = await screen.findByRole("region", { name: "Epic and legendary" });
    expect(await within(top).findByText("Epic one")).toBeInTheDocument();
    expect(within(top).getAllByText("13 Oct")).toHaveLength(2);
    expect(within(top).getByText("Gist of Epic one")).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Rare" })).toHaveTextContent("Rare one");
    const uncommon = screen.getByRole("region", { name: "Uncommon" });
    await within(uncommon).findByText("4");
    expect(within(uncommon).queryByRole("link")).toBeNull();
    expect(await screen.findByText("16 papers")).toBeInTheDocument();
    expect(screen.getByText("2 not yet classified")).toBeInTheDocument();
    // The folded shelf asked only for its count.
    const folded = calls.find((c) => c.url.includes("rarity=uncommon"));
    expect(folded?.url).toContain("limit=1");

    const star = within(top).getAllByRole("button", { name: "Save" })[1]!;
    await userEvent.click(star);
    await waitFor(() => expect(calls.some((c) => c.method === "PUT" && c.url === "/api/library/2610.00002")).toBe(true));
    expect(within(top).getByRole("button", { name: "Unsave" })).toBeInTheDocument();
    // Saving does not refetch the list, so rows keep their places.
    expect(calls.filter((c) => c.method === "GET" && c.url.startsWith("/api/papers")).length).toBe(4);
  });

  it("filters a rarity from the bar and puts it in the URL", async () => {
    viewport(true);
    scriptFetch({
      "GET /api/status": { state: "none", papers: 0, awaiting_classification: 0, analysis_failed: 0, ungraded: 0 },
      "GET /api/topics": [],
      "GET /api/papers": json({ rows: [], total: 0, counts }),
    });
    const { router } = renderApp("/papers");
    await userEvent.click(await screen.findByRole("button", { name: "Rare: 1" }));
    await waitFor(() => expect(router.state.location.searchStr).toBe("?rarity=rare"));
    expect(screen.queryByRole("region", { name: "Epic and legendary" })).toBeNull();
  });

  it("says when a shelf could not load and tries again on request", async () => {
    viewport(true);
    let failRare = true;
    const calls = scriptFetch({
      "GET /api/status": { state: "quiet", papers: 212, awaiting_classification: 0, analysis_failed: 0, ungraded: 0 },
      "GET /api/topics": [],
      "GET /api/papers": ({ url }) => {
        const rarity = new URL(url, "http://x").searchParams.get("rarity") ?? "";
        if (rarity === "rare" && failRare) return json({ detail: "the database went away" }, 500);
        if (rarity === "rare") return { rows: [row("2610.00003", "Rare one", "rare")], total: 1, counts };
        return { rows: [], total: rarity.includes("epic") ? 0 : 4, counts };
      },
    });
    renderApp("/papers");
    const rare = await screen.findByRole("region", { name: "Rare" });
    expect(await within(rare).findByText(/Could not load/)).toBeInTheDocument();
    failRare = false;
    await userEvent.click(within(rare).getByRole("button", { name: "Try again" }));
    expect(await within(rare).findByText("Rare one")).toBeInTheDocument();
    expect(calls.filter((c) => c.url.includes("rarity=rare")).length).toBe(2);
  });

  it("keeps the rows it has when the next page fails, and tries that page again on request", async () => {
    viewport(true);
    // jsdom has no IntersectionObserver; this one sees every sentinel at once.
    vi.stubGlobal(
      "IntersectionObserver",
      class {
        constructor(private cb: (entries: { isIntersecting: boolean }[]) => void) {}
        observe() {
          this.cb([{ isIntersecting: true }]);
        }
        disconnect() {}
      },
    );
    let failMore = true;
    const many = Array.from({ length: 50 }, (_, i) => row(`2610.${String(i).padStart(5, "0")}`, `Epic ${i}`, "epic"));
    const calls = scriptFetch({
      "GET /api/status": { state: "quiet", papers: 212, awaiting_classification: 0, analysis_failed: 0, ungraded: 0 },
      "GET /api/topics": [],
      "GET /api/papers": ({ url }) => {
        const q = new URL(url, "http://x").searchParams;
        if (!(q.get("rarity") ?? "").includes("epic")) return { rows: [], total: 0, counts };
        if (q.get("offset") === "0") return { rows: many, total: 51, counts };
        if (failMore) return json({ detail: "gone" }, 500);
        return { rows: [row("2610.00050", "Epic 50", "epic")], total: 51, counts };
      },
    });
    renderApp("/papers");
    const top = await screen.findByRole("region", { name: "Epic and legendary" });
    expect(await within(top).findByText(/Could not load more/)).toBeInTheDocument();
    expect(within(top).getAllByRole("link")).toHaveLength(50);
    failMore = false;
    await userEvent.click(within(top).getByRole("button", { name: "Try again" }));
    expect(await within(top).findByText("Epic 50")).toBeInTheDocument();
    expect(within(top).queryByText(/Could not load/)).toBeNull();
    expect(calls.filter((c) => c.url.includes("offset=50")).length).toBe(2);
  });

  it("keeps an opened shelf open on the way back, without refetching the list", async () => {
    viewport(true);
    const calls = scriptFetch({
      "GET /api/status": { state: "none", papers: 0, awaiting_classification: 0, analysis_failed: 0, ungraded: 0 },
      "GET /api/topics": [],
      "GET /api/papers": ({ url }) => {
        const q = new URL(url, "http://x").searchParams;
        if (q.get("rarity") !== "uncommon") return { rows: [], total: 0, counts };
        return q.get("limit") === "1" ? { rows: [], total: 1, counts } : { rows: [row("2610.00004", "Uncommon one", "uncommon")], total: 1, counts };
      },
    });
    // The app's own cache lifetime, so the list survives leaving the page as it does in use.
    const { router } = renderApp("/papers", new QueryClient({ defaultOptions: { queries: { retry: false } } }));
    const shelf = await screen.findByRole("region", { name: "Uncommon" });
    await userEvent.click(within(shelf).getByRole("button"));
    expect(await screen.findByText("Uncommon one")).toBeInTheDocument();
    const gets = () => calls.filter((c) => c.method === "GET" && c.url.startsWith("/api/papers")).length;
    const before = gets();
    await router.navigate({ to: "/topics" });
    await screen.findByText("No topics are followed yet.");
    router.history.back();
    expect(await screen.findByText("Uncommon one")).toBeInTheDocument();
    expect(gets()).toBe(before);
  });
});
