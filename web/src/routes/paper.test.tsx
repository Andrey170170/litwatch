import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient } from "@tanstack/react-query";
import { describe, expect, it } from "vitest";
import { renderApp, scriptFetch, viewport } from "@/test/harness";

const badge = { slug: "mi", name: "Mech interp", depth: "focus", monogram: "MI", colour: "#f3e600" };
const paper = {
  id: "2610.00002",
  title: "Epic one",
  authors: ["A. Author", "B. Author", "C. Author", "D. Author", "E. Author"],
  abstract: "We study things.",
  published: "2026-10-13",
  version: 1,
  versions: [{ version: 1, date: "2026-10-13" }],
  revision: 1,
  arxiv_url: "https://arxiv.org/abs/2610.00002",
  pdf_url: "https://arxiv.org/pdf/2610.00002",
  source_url: "https://arxiv.org/e-print/2610.00002",
  in_collection: true,
  rarity: "epic",
  badges: [{ ...badge, grade: "must_read", rarity: "epic" }],
  gist: { text: "The gist." },
  weight: { weight: "solid", reason: "r", source: "analysis" },
  facets: { contribution: "method", releases_code: false, releases_weights: false },
  saved: false,
  topics: [{ topic: badge, fit: "central", grade: "must_read", rarity: "epic", verdict: { fit: "central", reason: "why", split: false, escalated: false, opinions: {} }, missed: false }],
  states: [],
  publication_by_owner: false,
  card_requestable: true,
};
const status = { state: "quiet", papers: 1, awaiting_classification: 0, analysis_failed: 0, ungraded: 0 };

describe("a paper's page", () => {
  it("records a thumb only once a reason is picked, on the topic's row", async () => {
    viewport(true);
    const calls = scriptFetch({
      "GET /api/status": status,
      "GET /api/topics": [],
      "GET /api/papers/2610.00002": paper,
      "PUT /api/reactions/2610.00002": { message: "Overrated on Mech interp: central became related", saved: false, grades: { mi: "relevant" } },
    });
    renderApp("/paper/2610.00002");
    expect(await screen.findByRole("heading", { name: "Epic one" })).toBeInTheDocument();
    expect(screen.getAllByText("The gist.").length).toBeGreaterThan(0);
    expect(screen.getByRole("button", { name: "+2" })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Thumbs down" }));
    expect(calls.filter((c) => c.method === "PUT")).toHaveLength(0);
    await userEvent.click(screen.getByRole("button", { name: "Overrated" }));
    await waitFor(() => expect(calls.find((c) => c.method === "PUT")?.body).toEqual({ topic: "mi", value: "down", reason: "overrated" }));
    expect(await screen.findByRole("status")).toHaveTextContent("Overrated on Mech interp");
  });

  it("labels the Fit from the control and removes the label by tapping it again", async () => {
    viewport(true);
    const calls = scriptFetch({
      "GET /api/status": status,
      "GET /api/topics": [],
      "GET /api/papers/2610.00002": () => (calls.some((c) => c.method === "PUT") ? { ...paper, topics: [{ ...paper.topics[0], label: { value: "related" } }] } : paper),
      "PUT /api/labels/mi/2610.00002": { saved: false },
      "DELETE /api/labels/mi/2610.00002": { saved: false },
    });
    renderApp("/paper/2610.00002");
    await screen.findByRole("heading", { name: "Epic one" });
    await userEvent.click(screen.getByRole("button", { name: "Label" }));
    const fit = screen.getByRole("group", { name: "Fit for Mech interp" });
    await userEvent.click(screen.getByRole("button", { name: "related" }));
    await waitFor(() => expect(calls.find((c) => c.method === "PUT")?.body).toEqual({ value: "related" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "related" })).toHaveAttribute("aria-pressed", "true"));
    await userEvent.click(screen.getByRole("button", { name: "related" }));
    await waitFor(() => expect(calls.some((c) => c.method === "DELETE" && c.url === "/api/labels/mi/2610.00002")).toBe(true));
    expect(fit).toBeInTheDocument();
  });

  it("starts afresh on another paper: a DOI typed on one is not set on the next", async () => {
    viewport(true);
    const other = { ...paper, id: "2610.00003", title: "Epic two", arxiv_url: "https://arxiv.org/abs/2610.00003" };
    const calls = scriptFetch({
      "GET /api/status": status,
      "GET /api/topics": [],
      "GET /api/papers/2610.00002": paper,
      "GET /api/papers/2610.00003": other,
      "PUT /api/papers/2610.00003/publication": { venue: "ICLR", year: 2026, doi: "10.1/x" },
    });
    // The second paper stays cached (the harness's client forgets at once), so the page is reused on the way to it.
    const { router } = renderApp("/paper/2610.00003", new QueryClient({ defaultOptions: { queries: { retry: false } } }));
    expect(await screen.findByRole("heading", { name: "Epic two" })).toBeInTheDocument();
    await router.navigate({ to: "/paper/$", params: { _splat: "2610.00002" } });
    expect(await screen.findByRole("heading", { name: "Epic one" })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Paste a DOI" }));
    await userEvent.type(screen.getByRole("textbox", { name: "DOI" }), "10.1/x");
    await router.navigate({ to: "/paper/$", params: { _splat: "2610.00003" } });
    expect(await screen.findByRole("heading", { name: "Epic two" })).toBeInTheDocument();
    expect(screen.queryByRole("textbox", { name: "DOI" })).toBeNull();
    expect(screen.getByRole("button", { name: "Paste a DOI" })).toBeInTheDocument();
    expect(calls.filter((c) => c.method === "PUT")).toHaveLength(0);
  });

  it("says when a paper is not in the collection", async () => {
    viewport(true);
    scriptFetch({
      "GET /api/status": status,
      "GET /api/topics": [],
      "GET /api/papers/9999.99999": () => new Response(JSON.stringify({ detail: "not found" }), { status: 404 }),
    });
    renderApp("/paper/9999.99999");
    expect(await screen.findByText("9999.99999 is not in the collection.")).toBeInTheDocument();
  });
});
