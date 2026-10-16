import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient } from "@tanstack/react-query";
import { describe, expect, it } from "vitest";
import { json, renderApp, scriptFetch, viewport } from "@/test/harness";

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
const status = { state: "quiet", papers: 1, awaiting_classification: 0, analysis_failed: 0, ungraded: 0, fulltext_failed: 0, digests_failed: 0 };
const brief = { id: 1, text: "The brief, from the full text.", overstatement: "The abstract overstates: the gain holds only for 7B models.", revision: 1, arxiv_version: 1, source: "latex", written_at: "2026-10-14T03:00:00Z" };

describe("a paper's page", () => {
  it("shows the Brief in the abstract's place, its overstatement first, and the abstract only without one", async () => {
    viewport(true);
    scriptFetch({ "GET /api/status": status, "GET /api/topics": [], "GET /api/papers/2610.00002": { ...paper, brief } });
    renderApp("/paper/2610.00002");
    await screen.findByRole("heading", { name: "Epic one" });
    expect(screen.getByText("The brief, from the full text.")).toBeInTheDocument();
    const callout = screen.getByText("The abstract overstates: the gain holds only for 7B models.");
    expect(callout.compareDocumentPosition(screen.getByText("The brief, from the full text."))).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
    expect(screen.queryByText("We study things.")).toBeNull();
    expect(screen.queryByText(/No Brief yet/)).toBeNull();
    expect(screen.queryByText(/older revision/)).toBeNull();
  });

  it("says which revision an older Brief read, and why the abstract stands in without one", async () => {
    viewport(true);
    scriptFetch({
      "GET /api/status": status,
      "GET /api/topics": [],
      "GET /api/papers/2610.00002": { ...paper, revision: 2, brief },
      "GET /api/papers/2610.00003": { ...paper, id: "2610.00003", text_pass: { id: 7, kind: "brief", state: "awaiting_text", queued_at: "2026-10-14T03:00:00Z" } },
    });
    renderApp("/paper/2610.00002");
    await screen.findByRole("heading", { name: "Epic one" });
    expect(screen.getByText(/Written for revision 1 of the text \(arXiv v1\); the current one, revision 2, waits for its full text/)).toBeInTheDocument();
    renderApp("/paper/2610.00003");
    await waitFor(() => expect(screen.getByText("The Brief waits for the paper's full text; the abstract stands in.")).toBeInTheDocument());
    expect(screen.getByText("We study things.")).toBeInTheDocument();
  });

  it("runs a stuck full-text pass now from its note, and says so when the page is behind", async () => {
    viewport(true);
    const states = [
      { kind: "fulltext_failed", revision: 1, error: "the LaTeX did not build", pass_id: 7 },
      { kind: "awaiting_text", revision: 1, error: null, pass_id: 8 },
    ];
    const calls = scriptFetch({
      "GET /api/status": status,
      "GET /api/topics": [],
      // Each run drops its pass from the page: the first on success, the second on a 409 (the page was behind).
      "GET /api/papers/2610.00002": () => ({ ...paper, states: states.slice(calls.filter((c) => c.method === "POST").length), text_pass: { id: 7, kind: "brief", state: "failed", queued_at: "2026-10-14T03:00:00Z" } }),
      "POST /api/settings/fulltext/7/now": () => new Response(null, { status: 204 }),
      "POST /api/settings/fulltext/8/now": () => json({ detail: "the pass is not waiting for the owner" }, 409),
    });
    // The Needs you page the reader came from lists both passes; each is settled there as it leaves the paper.
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const row = (id: number, state: string) => ({ id, paper_id: "2610.00002", revision: 1, title: "Epic one", kind: "brief", state, failed_runs: 0, last_error: null, queued_at: "2026-10-08T03:00:00Z" });
    client.setQueryData(["settings", "needs", "quality"], { night: { run: 1, error: null, splits: [], awaiting_classification: [], analysis_failed: [], ungraded: [], fulltext: [row(7, "failed"), row(8, "awaiting_text")], digests: [] }, cost: [], recall: null, calibration: [], matters_no_topic: [], collected: {}, reactions: [] });
    const settledRows = () => (client.getQueryData(["settings", "needs", "quality"]) as { night: { fulltext: { id: number; settled?: string }[] } }).night.fulltext.map((r) => r.settled);
    renderApp("/paper/2610.00002", client);
    await screen.findByRole("heading", { name: "Epic one" });
    expect(screen.getByText(/Full text failed three runs in a row/)).toBeInTheDocument();
    expect(screen.getByText("the LaTeX did not build")).toBeInTheDocument();
    expect(screen.getByText("The full-text pass failed three runs in a row; the abstract stands in until it is run again.")).toBeInTheDocument();
    expect(screen.getByText(/Awaiting the full text for over a week/)).toBeInTheDocument();
    const [failed] = screen.getAllByRole("button", { name: "Run it now" });
    await userEvent.click(failed!);
    await waitFor(() => expect(calls.some((c) => c.method === "POST" && c.url === "/api/settings/fulltext/7/now")).toBe(true));
    // The page is refetched: the pass that ran is gone from the notes, the other's note stays. The row on Needs you
    // settles from the write itself, with what happened, as it would have from that page.
    await waitFor(() => expect(screen.queryByText(/Full text failed three runs in a row/)).toBeNull());
    expect(settledRows()).toEqual(["Running now, first in line, with three more runs.", undefined]);
    await userEvent.click(screen.getByRole("button", { name: "Run it now" }));
    expect(await screen.findByText("the pass is not waiting for the owner")).toBeInTheDocument();
    // The 409 settles the row on Needs you with its reason, and the refetch finds the pass gone from the page too.
    await waitFor(() => expect(settledRows()).toEqual(["Running now, first in line, with three more runs.", "No longer waits on the owner: the pass is not waiting for the owner."]));
    await waitFor(() => expect(screen.queryByText(/Awaiting the full text for over a week/)).toBeNull());
  });

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

  it("keeps a Deep read asked from a sheet that closed before the request returned", async () => {
    viewport(false);
    let release = () => {};
    const held = new Promise<void>((r) => (release = r));
    scriptFetch({
      "GET /api/status": status,
      "GET /api/topics": [],
      "GET /api/papers/2610.00002": paper,
      "POST /api/papers/2610.00002/card": async () => {
        await held;
        return { message: "queued" };
      },
    });
    renderApp("/paper/2610.00002");
    await screen.findByRole("heading", { name: "Epic one" });
    await userEvent.click(screen.getByRole("button", { name: "More" }));
    const sheet = screen.getByRole("dialog", { name: "The paper" });
    await userEvent.click(within(sheet).getByRole("button", { name: "Deep read" }));
    await userEvent.click(within(sheet).getByRole("button", { name: "Done" }));
    expect(screen.queryByRole("dialog")).toBeNull();
    release();
    expect(await screen.findByText(/Queued · the card shows here/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Deep read" })).toBeNull();
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

  it("says the archive did not answer, not that the paper is missing, and tries again", async () => {
    viewport(true);
    let down = true;
    scriptFetch({
      "GET /api/status": status,
      "GET /api/topics": [],
      "GET /api/papers/2610.00002": () => (down ? json({ detail: "The archive did not answer" }, 503) : paper),
    });
    renderApp("/paper/2610.00002");
    const said = await screen.findByText(/The archive did not answer/);
    expect(screen.queryByText(/not in the collection/)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Process" })).not.toBeInTheDocument();
    down = false;
    await userEvent.click(within(said).getByRole("button", { name: "Try again" }));
    expect(await screen.findByRole("heading", { name: "Epic one" })).toBeInTheDocument();
  });

  it("fetches a paper it does not hold from its own page, then shows it", async () => {
    viewport(true);
    let held = true;
    const calls = scriptFetch({
      "GET /api/status": status,
      "GET /api/topics": [],
      "GET /api/papers/2409.01234": () => (held ? json({ detail: "no such paper" }, 404) : { ...paper, id: "2409.01234", title: "Fetched one", in_collection: false, topics: [], badges: [], rarity: null, gist: null }),
      "POST /api/lookup": () => {
        held = false;
        return { fetched: true };
      },
    });
    renderApp("/paper/2409.01234");
    await screen.findByText("2409.01234 is not in the collection.");
    await userEvent.click(screen.getByRole("button", { name: "Process" }));
    expect(await screen.findByRole("heading", { name: "Fetched one" })).toBeInTheDocument();
    expect(calls.find((c) => c.method === "POST")?.body).toEqual({ q: "2409.01234" });
  });

  it("shows an archive paper litwatch has not fetched, and the paper fetched once Process brings it in", async () => {
    viewport(true);
    let stored = false;
    const outside = { ...paper, id: "2409.01234", title: "Archive one", in_collection: false, topics: [], badges: [], rarity: null, gist: null, weight: null, facets: null };
    const calls = scriptFetch({
      "GET /api/status": status,
      "GET /api/topics": [],
      "GET /api/papers/2409.01234": () => ({ ...outside, stored }),
      "POST /api/lookup": () => {
        stored = true;
        return { fetched: true };
      },
    });
    renderApp("/paper/2409.01234");
    expect(await screen.findByRole("heading", { name: "Archive one" })).toBeInTheDocument();
    expect(screen.getByText("We study things.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Paste a DOI" })).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Process" }));
    expect(await screen.findByRole("button", { name: "Fetched · next run" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Paste a DOI" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Process" })).toBeNull();
    expect(calls.filter((c) => c.method === "POST")).toHaveLength(1);
  });

  it("cites a paper litwatch has not fetched from its page on the phone, with no DOI form", async () => {
    viewport(false);
    const outside = { ...paper, id: "2409.01234", title: "Archive one", in_collection: false, stored: false, topics: [], badges: [], rarity: null, gist: null, weight: null, facets: null };
    const calls = scriptFetch({
      "GET /api/status": status,
      "GET /api/topics": [],
      "GET /api/papers/2409.01234": outside,
      "POST /api/bibtex": () => new Response("@misc{author2026archive}", { headers: { "content-type": "text/plain" } }),
    });
    renderApp("/paper/2409.01234");
    await screen.findByRole("heading", { name: "Archive one" });
    await userEvent.click(screen.getByRole("button", { name: "More" }));
    const sheet = screen.getByRole("dialog", { name: "The paper" });
    expect(within(sheet).queryByRole("button", { name: "Paste a DOI" })).toBeNull();
    await userEvent.click(within(sheet).getByRole("button", { name: /BibTeX/ }));
    await waitFor(() => expect(calls.some((c) => c.url === "/api/bibtex")).toBe(true));
    expect(calls.find((c) => c.url === "/api/bibtex")?.body).toEqual({ cited: [{ id: "2409.01234", title: "Archive one", authors: outside.authors, year: 2026 }] });
  });

  it("runs a failed full-text pass again on the paper's PDF", async () => {
    viewport(true);
    const failed = { kind: "fulltext_failed", revision: 1, error: "LaTeX reads badly", pass_id: 7 };
    const calls = scriptFetch({
      "GET /api/status": status,
      "GET /api/topics": [],
      "GET /api/papers/2610.00002": () => ({ ...paper, states: calls.some((c) => c.method === "POST") ? [] : [failed] }),
      "POST /api/settings/fulltext/7/pdf": () => new Response(null, { status: 204 }),
    });
    renderApp("/paper/2610.00002");
    expect(await screen.findByText("Full text failed three runs in a row.")).toBeInTheDocument();
    expect(screen.getByText("LaTeX reads badly")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Run it now" })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Use the PDF" }));
    await waitFor(() => expect(screen.queryByText("Full text failed three runs in a row.")).toBeNull());
    expect(calls.filter((c) => c.method === "POST").map((c) => c.url)).toEqual(["/api/settings/fulltext/7/pdf"]);
    expect(await screen.findByText("Running the pass now, on the PDF; its result shows here once written")).toBeInTheDocument();
  });

  it("lights the phone's thumbs for the topic the paper was opened under, the one its sheet opens on", async () => {
    viewport(false);
    const other = { slug: "al", name: "Alignment", depth: "focus", monogram: "AL", colour: "#ff3b4e" };
    const two = {
      ...paper,
      topics: [
        { ...paper.topics[0], reaction: null },
        { topic: other, fit: "related", grade: "relevant", rarity: "rare", verdict: null, missed: false, reaction: { value: "down", reason: "overrated" } },
      ],
    };
    scriptFetch({ "GET /api/status": status, "GET /api/topics": [], "GET /api/papers/2610.00002": two });
    renderApp("/paper/2610.00002?topic=mi");
    await screen.findByRole("heading", { name: "Epic one" });
    expect(screen.getByRole("button", { name: "Thumbs down" })).toHaveClass("dim");
    await userEvent.click(screen.getByRole("button", { name: "Thumbs down" }));
    const sheet = screen.getByRole("dialog", { name: "Thumbs down" });
    expect(within(sheet).getByRole("button", { name: /Mech interp/ })).toHaveAttribute("aria-pressed", "true");
  });

  it("gives focus back to the control that opened a sheet", async () => {
    viewport(false);
    scriptFetch({ "GET /api/status": status, "GET /api/topics": [], "GET /api/papers/2610.00002": paper });
    renderApp("/paper/2610.00002");
    await screen.findByRole("heading", { name: "Epic one" });
    const more = screen.getByRole("button", { name: "More" });
    await userEvent.click(more);
    await userEvent.click(within(screen.getByRole("dialog", { name: "The paper" })).getByRole("button", { name: "Done" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(document.activeElement).toBe(more);
  });
});
