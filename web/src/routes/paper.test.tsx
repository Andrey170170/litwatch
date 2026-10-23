import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient } from "@tanstack/react-query";
import { describe, expect, it, onTestFinished } from "vitest";
import { freshClient, json, renderApp, scriptFetch, viewport } from "@/test/harness";
import type { Paper } from "@/components/paper/Actions";
import { pollEvery } from "./paper";

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
/** Whether a text is on the page and not in the hidden half of the Brief rung's switch. */
const shown = (text: string) => screen.queryAllByText(text).some((el) => !el.closest('[aria-hidden="true"]'));
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
    expect(shown("We study things.")).toBe(false);
    expect(screen.queryByText(/No Brief yet/)).toBeNull();
    expect(screen.queryByText(/older revision/)).toBeNull();
  });

  it("labels a full-text Gist with its Brief's source, the one text both were written from", async () => {
    viewport(true);
    const gist = { text: "The gist.", source: "full_text" };
    scriptFetch({
      "GET /api/status": status,
      "GET /api/topics": [],
      "GET /api/papers/2610.00002": { ...paper, gist, brief: { ...brief, source: "pdf" } },
      "GET /api/papers/2610.00003": { ...paper, id: "2610.00003", gist, brief },
    });
    const { router } = renderApp("/paper/2610.00002");
    await screen.findByRole("heading", { name: "Epic one" });
    expect(screen.getAllByText("from the PDF")).toHaveLength(2);
    expect(screen.queryByText("from the full text")).toBeNull();
    await router.navigate({ to: "/paper/$", params: { _splat: "2610.00003" } });
    await waitFor(() => expect(screen.getAllByText("from the LaTeX")).toHaveLength(2));
    expect(screen.queryByText("from the PDF")).toBeNull();
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
    expect(shown("We study things.")).toBe(true);
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
    client.setQueryData(["settings", "needs", "quality"], { night: { run: 1, error: null, splits: [], awaiting_classification: [], analysis_failed: [], ungraded: [], fulltext: [row(7, "failed"), row(8, "awaiting_text")], digests: [], deep_notes: [] }, cost: [], recall: null, calibration: [], matters_no_topic: [], collected: {}, reactions: [] });
    const settledRows = () => (client.getQueryData(["settings", "needs", "quality"]) as { night: { fulltext: { id: number; settled?: string }[] } }).night.fulltext.map((r) => r.settled);
    // Jobs read before lists the paper's card asked for: it is read again after the write.
    client.setQueryData(["settings", "runs/jobs", "jobs"], { not_run: [], running: [], failed: [], done: [] });
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
    expect(client.getQueryState(["settings", "runs/jobs", "jobs"])?.isInvalidated).toBe(true);
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
        return { pass_id: 73 };
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
    expect(await screen.findByText(/Asked for · queued · the card shows here/)).toBeInTheDocument();
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

  it("fetches a paper it does not hold from its own page and starts its job, then shows it processing", async () => {
    viewport(true);
    let held = true;
    const job = { id: 4, state: "queued", error: null };
    const calls = scriptFetch({
      "GET /api/status": status,
      "GET /api/topics": [],
      "GET /api/papers/2409.01234": () => (held ? json({ detail: "no such paper" }, 404) : { ...paper, id: "2409.01234", title: "Fetched one", in_collection: false, topics: [], badges: [], rarity: null, gist: null, process: job }),
      "POST /api/jobs/process": () => {
        held = false;
        return json(job, 202);
      },
    });
    renderApp("/paper/2409.01234");
    await screen.findByText("2409.01234 is not in the collection.");
    await userEvent.click(screen.getByRole("button", { name: "Process" }));
    expect(await screen.findByRole("heading", { name: "Fetched one" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Processing" })).toBeDisabled();
    expect(calls.find((c) => c.method === "POST")?.body).toEqual({ paper_id: "2409.01234" });
  });

  it("shows an archive paper litwatch has not fetched, processing once Process starts its job, then processed", async () => {
    viewport(true);
    let job: { id: number; state: string; error: null } | null = null;
    const outside = { ...paper, id: "2409.01234", title: "Archive one", in_collection: false, topics: [], badges: [], rarity: null, gist: null, weight: null, facets: null };
    const calls = scriptFetch({
      "GET /api/status": status,
      "GET /api/topics": [],
      "GET /api/papers/2409.01234": () => (job?.state === "done" ? paper : { ...outside, stored: !!job, next_run: !!job, process: job }),
      "POST /api/jobs/process": () => {
        job = { id: 4, state: "queued", error: null };
        return json(job, 202);
      },
    });
    const client = freshClient();
    renderApp("/paper/2409.01234", client);
    expect(await screen.findByRole("heading", { name: "Archive one" })).toBeInTheDocument();
    expect(screen.getByText("We study things.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Paste a DOI" })).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Process" }));
    expect(await screen.findByRole("button", { name: "Processing" })).toBeDisabled();
    expect(screen.queryByRole("button", { name: /next run/ })).toBeNull();
    expect(await screen.findByText(/Processing it now; Settings › Runs and cost › Jobs/)).toBeInTheDocument();
    // the page polls while the job is under way; done, the paper is in the collection
    expect(pollEvery(client.getQueryData<Paper>(["paper", "2409.01234"]), false)).toBe(15_000);
    job = { id: 4, state: "done", error: null };
    await client.refetchQueries({ queryKey: ["paper", "2409.01234"] });
    expect(await screen.findByRole("heading", { name: "Epic one" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Processing" })).toBeNull();
    expect(calls.filter((c) => c.method === "POST")).toHaveLength(1);
  });

  it("offers Process again once a paper's job failed, with why", async () => {
    viewport(true);
    const failed = { ...paper, id: "2409.01234", title: "Failed one", in_collection: false, stored: true, next_run: true, topics: [], badges: [], rarity: null, gist: null, weight: null, facets: null, process: { id: 4, state: "failed", error: "classification failed on 1", again: true } };
    const calls = scriptFetch({ "GET /api/status": status, "GET /api/topics": [], "GET /api/papers/2409.01234": failed, "POST /api/jobs/process": json({ id: 4, state: "queued", error: null }, 202) });
    renderApp("/paper/2409.01234");
    const again = await screen.findByRole("button", { name: "Process again" });
    expect(again).toHaveAttribute("title", "Processing failed: classification failed on 1");
    await userEvent.click(again);
    await waitFor(() => expect(calls.filter((c) => c.method === "POST")).toHaveLength(1));
  });

  it("offers no Process again while the failed job's paper waits on the owner: the note above does", async () => {
    viewport(true);
    const waits = { kind: "awaiting_classification", revision: 1, error: "no usable answer", refused: false };
    const stuck = { ...paper, id: "2409.01234", title: "Stuck one", in_collection: false, stored: true, next_run: false, topics: [], badges: [], rarity: null, gist: null, weight: null, facets: null, states: [waits], process: { id: 4, state: "failed", error: "classification failed on 1 (waits on Needs you)", again: false } };
    scriptFetch({ "GET /api/status": status, "GET /api/topics": [], "GET /api/papers/2409.01234": stuck });
    renderApp("/paper/2409.01234");
    const failed = await screen.findByRole("button", { name: "Failed" });
    expect(failed).toBeDisabled();
    expect(failed).toHaveAttribute("title", "Processing failed and waits on you: classification failed on 1 (waits on Needs you)");
    expect(screen.queryByRole("button", { name: "Process again" })).toBeNull();
    expect(screen.getByRole("button", { name: "Classify by hand" })).toBeInTheDocument();
  });

  it("keeps saying Processing once the classified paper is in the collection, until its job is done", async () => {
    viewport(false);
    let state = "running";
    scriptFetch({ "GET /api/status": status, "GET /api/topics": [], "GET /api/papers/2409.01234": () => ({ ...paper, id: "2409.01234", title: "Classified one", process: { id: 4, state, error: null } }) });
    const qc = freshClient();
    renderApp("/paper/2409.01234", qc);
    await screen.findByRole("heading", { name: "Classified one" });
    expect(screen.getByText("Processing.")).toBeInTheDocument();
    expect(screen.getByText(/follow in a minute or two/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Jobs" })).toHaveAttribute("href", "/settings/runs/jobs");
    // Put on tonight's run, it promises no minute or two.
    state = "tonight";
    await qc.invalidateQueries();
    expect(await screen.findByText(/follow after tonight's run/)).toBeInTheDocument();
    state = "done";
    await qc.invalidateQueries();
    await waitFor(() => expect(screen.queryByText("Processing.")).toBeNull());
  });

  it("offers Try again on a Process job that failed after classifying the paper, and then says Processing", async () => {
    viewport(true);
    let job: { id: number; state: string; error: string | null; again: boolean } = { id: 4, state: "failed", error: "analysis failed on 1", again: true };
    const calls = scriptFetch({
      "GET /api/status": status,
      "GET /api/topics": [],
      "GET /api/papers/2409.01234": () => ({ ...paper, id: "2409.01234", title: "Half done", states: [], process: job }),
      "POST /api/jobs/4/again": () => {
        job = { id: 4, state: "queued", error: null, again: false };
        return json({ not_run: [], running: [], failed: [], done: [] });
      },
    });
    renderApp("/paper/2409.01234");
    const note = (await screen.findByText("Processing failed.")).closest(".note") as HTMLElement;
    expect(within(note).getByText("analysis failed on 1")).toBeInTheDocument();
    await userEvent.click(within(note).getByRole("button", { name: "Try again" }));
    expect(await screen.findByText("Processing.")).toBeInTheDocument();
    expect(screen.queryByText("Processing failed.")).toBeNull();
    expect(calls.filter((c) => c.method === "POST")).toHaveLength(1);
  });

  it("offers Process on a fetched paper never processed while the API says it would run now, else the next run", async () => {
    viewport(true);
    const fetched = { ...paper, id: "2409.01234", title: "Fetched one", in_collection: false, stored: true, next_run: true, topics: [], badges: [], rarity: null, gist: null, weight: null, facets: null };
    const calls = scriptFetch({
      "GET /api/status": status,
      "GET /api/topics": [],
      "GET /api/papers/2409.01234": { ...fetched, process_now: true },
      "GET /api/papers/2409.01235": { ...fetched, id: "2409.01235", title: "Waiting one", process_now: false },
      "POST /api/jobs/process": json({ id: 4, state: "queued", error: null }, 202),
    });
    renderApp("/paper/2409.01234");
    await screen.findByRole("heading", { name: "Fetched one" });
    expect(screen.queryByRole("button", { name: /next run/ })).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Process" }));
    await waitFor(() => expect(calls.find((c) => c.method === "POST")?.body).toEqual({ paper_id: "2409.01234" }));

    renderApp("/paper/2409.01235");
    await screen.findByRole("heading", { name: "Waiting one" });
    expect(screen.getByRole("button", { name: "Fetched · next run" })).toBeDisabled();
  });

  it("says a fetched paper no run will process is fetched, without promising the next run", async () => {
    viewport(true);
    const dismissed = { ...paper, id: "2409.01234", title: "Dismissed one", in_collection: false, stored: true, next_run: false, topics: [], badges: [], rarity: null, gist: null, weight: null, facets: null };
    scriptFetch({ "GET /api/status": status, "GET /api/topics": [], "GET /api/papers/2409.01234": dismissed });
    renderApp("/paper/2409.01234");
    expect(await screen.findByRole("button", { name: "Fetched" })).toBeDisabled();
    expect(screen.queryByRole("button", { name: /next run/ })).toBeNull();
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

  it("cites a paper litwatch has not fetched from its rail on the desktop", async () => {
    viewport(true);
    const outside = { ...paper, id: "2409.01234", title: "Archive one", in_collection: false, stored: false, topics: [], badges: [], rarity: null, gist: null, weight: null, facets: null };
    const calls = scriptFetch({
      "GET /api/status": status,
      "GET /api/topics": [],
      "GET /api/papers/2409.01234": outside,
      "POST /api/bibtex": () => new Response("@misc{author2026archive}", { headers: { "content-type": "text/plain" } }),
    });
    renderApp("/paper/2409.01234");
    await screen.findByRole("heading", { name: "Archive one" });
    await userEvent.click(screen.getByRole("button", { name: /BibTeX/ }));
    await waitFor(() => expect(calls.find((c) => c.url === "/api/bibtex")?.body).toEqual({ cited: [{ id: "2409.01234", title: "Archive one", authors: outside.authors, year: 2026 }] }));
  });

  it("polls while a Brief or a Deep card pass is queued or running, not while it waits on the owner", () => {
    const pass = (state: string) => ({ id: 9, reason: "request", state, queued_at: "2026-10-14T03:00:00Z" });
    const waits = (kind: string, pass_id: number) => ({ kind, revision: 1, pass_id });
    const at = (fields: object) => pollEvery({ ...paper, states: [], ...fields } as unknown as Paper, false);
    expect(at({ card_pass: pass("failed"), states: [waits("card_failed", 9)] })).toBe(false);
    // Run it now from the card_failed note: the refetched page has the pass queued again, and polls for the card.
    expect(at({ card_pass: pass("ready") })).toBe(15_000);
    expect(at({ card_pass: pass("running") })).toBe(15_000);
    expect(at({ card_pass: pass("awaiting_text") })).toBe(60_000);
    expect(at({ card_pass: pass("awaiting_text"), states: [waits("awaiting_text", 9)] })).toBe(false);
    // A Brief waiting on the owner does not stop the poll for a card pass that is not.
    const brief = { id: 7, kind: "brief", state: "awaiting_text", queued_at: "2026-10-14T03:00:00Z" };
    expect(at({ text_pass: brief, card_pass: pass("awaiting_text"), states: [waits("awaiting_text", 7)] })).toBe(60_000);
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

  it("shows a Deep card's Among the collection rows, what it was written from, and Write again", async () => {
    viewport(true);
    const card = {
      card: { key_claim: "A meta-verifier catches 71% of unfaithful chains.", summary: "SIFT trains a probe.", prior_work: [{ title: "Measuring Faithfulness", relation: "extends", arxiv_id: "2307.13702" }] },
      model: "claude-fable-5-1",
      written_at: "2026-10-15T21:10:00Z",
      revision: 1,
      arxiv_version: 1,
      source: "latex",
      among: [
        { id: "2610.18105", title: "CoT Monitoring Can Fail", relation: "agrees", note: "Both find monitors miss steps.", rarity: "epic", in_collection: true },
        { id: "2501.00001", title: "An outside paper", relation: "contradicts", note: "Finds the opposite.", rarity: null, in_collection: false },
      ],
    };
    let reads = 0;
    const pass = { id: 74, reason: "request", state: "ready", queued_at: "2026-10-15T21:20:00Z", failed_runs: 0, error: null };
    scriptFetch({ "GET /api/status": status, "GET /api/topics": [], "GET /api/papers/2610.00002": () => (++reads === 1 ? { ...paper, card } : { ...paper, card, card_requestable: false, card_pass: pass }), "POST /api/papers/2610.00002/card": { pass_id: 74 } });
    renderApp("/paper/2610.00002");
    await screen.findByRole("heading", { name: "Epic one" });
    const among = screen.getByText("Among the collection").closest(".rung")!;
    expect(within(among as HTMLElement).getByRole("link", { name: /CoT Monitoring Can Fail/ })).toHaveAttribute("href", "/paper/2610.18105");
    expect(within(among as HTMLElement).getByText("agrees")).toBeInTheDocument();
    expect(within(among as HTMLElement).getByText("outside the collection")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Measuring Faithfulness.*2307\.13702/ })).toHaveAttribute("href", "/paper/2307.13702");
    expect(screen.getByText(/Written from the LaTeX source of arXiv v1 \(text revision 1\), .*, by claude-fable-5-1/)).toBeInTheDocument();
    // Write again asks for a new card over this one; the card keeps its label, and the pass read back shows under it.
    await userEvent.click(screen.getAllByRole("button", { name: "Write again" })[0]!);
    expect(await screen.findByText("Writing again · queued")).toBeInTheDocument();
    expect(screen.getByText(/Written from the LaTeX source/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Write again" })).toBeNull();
  });

  it("folds a Deep card's process notes after its Limitations, each with its kind, linking only an http(s) page", async () => {
    viewport(true);
    const notes = [
      { kind: "access", source: "model", note: "The linked repository could not be listed, so code is unverified.", url: "https://anonymous.4open.science/r/abc" },
      { kind: "tool", source: "model", note: "A table could not be parsed from the PDF text.", url: "javascript:alert(1)" },
    ];
    const card = { card: { key_claim: "A meta-verifier catches 71% of unfaithful chains.", limitations: ["Two models only."], prior_work: [{ title: "Measuring Faithfulness", relation: "extends" }], process_notes: notes }, written_at: "2026-10-20T03:00:00Z" };
    scriptFetch({ "GET /api/status": status, "GET /api/topics": [], "GET /api/papers/2610.00002": { ...paper, card } });
    renderApp("/paper/2610.00002");
    const fold = await screen.findByRole("button", { name: "Process notes · 2" });
    expect(fold).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByText(/could not be listed/)).toBeNull();
    // Its place: after Limitations, before Prior work.
    const rungs = [...document.querySelectorAll(".rung")];
    const at = (el: Element) => rungs.indexOf(el.closest(".rung")!);
    expect(at(screen.getByText("Limitations"))).toBeLessThan(at(fold));
    expect(at(fold)).toBeLessThan(at(screen.getByText("Prior work")));
    await userEvent.click(fold);
    expect(fold).toHaveAttribute("aria-expanded", "true");
    const rung = fold.closest(".rung") as HTMLElement;
    expect(within(rung).getByText("access")).toHaveClass("tag");
    expect(within(rung).getByText("tool")).toHaveClass("tag");
    expect(within(rung).getByRole("link", { name: /anonymous\.4open\.science/ })).toHaveAttribute("href", "https://anonymous.4open.science/r/abc");
    expect(within(rung).getAllByRole("link")).toHaveLength(1);
    expect(rung.querySelector('a[href^="javascript"]')).toBeNull();
  });

  it("says why a card is coming and where it stands while its pass is open", async () => {
    viewport(true);
    const pass = { id: 73, reason: "pick", state: "running", queued_at: "2026-10-15T21:00:00Z", failed_runs: 0, error: null };
    scriptFetch({
      "GET /api/status": status,
      "GET /api/topics": [],
      "GET /api/papers/2610.00002": { ...paper, card_pass: pass },
      "GET /api/papers/2610.00003": { ...paper, id: "2610.00003", card_requestable: false, card_pass: { ...pass, reason: "request", state: "failed", failed_runs: 3, error: "timed out" } },
    });
    const { unmount } = renderApp("/paper/2610.00002");
    await screen.findByRole("heading", { name: "Epic one" });
    expect(screen.getByText("Automatic for an Area pick · being written")).toBeInTheDocument();
    // An automatic pass is the owner's to take over: Deep read stays live.
    expect(screen.getByRole("button", { name: "Deep read" })).toBeEnabled();
    unmount();
    renderApp("/paper/2610.00003");
    await screen.findByRole("heading", { name: "Epic one" });
    expect(screen.getByText("Asked for · failed three runs in a row")).toBeInTheDocument();
  });

  it("takes an automatic pass over: the request is remembered until the pass read back is the owner's", async () => {
    viewport(true);
    const pass = { id: 73, reason: "pick", state: "failed", queued_at: "2026-10-15T21:00:00Z", failed_runs: 1, error: "timed out" };
    let reads = 0;
    scriptFetch({
      "GET /api/status": status,
      "GET /api/topics": [],
      // the first read shows the failed automatic pass; the one after the request shows it taken over and queued
      "GET /api/papers/2610.00002": () => (++reads === 1 ? { ...paper, card_pass: pass } : { ...paper, card_requestable: false, card_pass: { ...pass, reason: "request", state: "ready", failed_runs: 0, error: null } }),
      "POST /api/papers/2610.00002/card": { pass_id: 73 },
    });
    renderApp("/paper/2610.00002");
    await screen.findByText("Automatic for an Area pick · failed; it tries again on the next run (1 of 3)");
    await userEvent.click(screen.getByRole("button", { name: "Deep read" }));
    expect(await screen.findByText("Asked for · queued")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Deep read · queued" })).toBeDisabled();
    expect(reads).toBe(2);
  });
});

describe("a paper's Brief and abstract", () => {
  it("opens on the Brief, switches to the abstract in its place, and opens on the Brief again on the next paper", async () => {
    viewport(true);
    scriptFetch({
      "GET /api/status": status,
      "GET /api/topics": [],
      "GET /api/papers/2610.00002": { ...paper, brief },
      "GET /api/papers/2610.00003": { ...paper, id: "2610.00003", title: "Epic two", brief },
    });
    const { router } = renderApp("/paper/2610.00002");
    await screen.findByRole("heading", { name: "Epic one" });
    const show = screen.getByRole("group", { name: "Show" });
    expect(within(show).getByRole("button", { name: "Brief" })).toHaveAttribute("aria-pressed", "true");
    expect(shown("The brief, from the full text.")).toBe(true);
    expect(shown("We study things.")).toBe(false);
    await userEvent.click(within(show).getByRole("button", { name: "Abstract" }));
    expect(shown("We study things.")).toBe(true);
    // the overstatement and the revision belong to the Brief's view
    expect(shown("The brief, from the full text.")).toBe(false);
    expect(shown("The abstract overstates: the gain holds only for 7B models.")).toBe(false);
    await router.navigate({ to: "/paper/$", params: { _splat: "2610.00003" } });
    await screen.findByRole("heading", { name: "Epic two" });
    expect(within(screen.getByRole("group", { name: "Show" })).getByRole("button", { name: "Brief" })).toHaveAttribute("aria-pressed", "true");
    expect(shown("The brief, from the full text.")).toBe(true);
  });

  it("has no switch without a Brief", async () => {
    viewport(false);
    scriptFetch({ "GET /api/status": status, "GET /api/topics": [], "GET /api/papers/2610.00002": paper });
    renderApp("/paper/2610.00002");
    await screen.findByRole("heading", { name: "Epic one" });
    expect(screen.queryByRole("group", { name: "Show" })).toBeNull();
    expect(screen.getByText("Abstract")).toBeInTheDocument();
    expect(shown("We study things.")).toBe(true);
  });
});

describe("a paper's topic rows", () => {
  const topic = (slug: string, name: string, grade: string | null, rarity: string | null) => ({ topic: { ...badge, slug, name, monogram: slug.toUpperCase() }, fit: grade ? "related" : null, grade, rarity, verdict: null, missed: false });
  // in the API's order: Focus, Area, Field
  const many = {
    ...paper,
    topics: [topic("aa", "Adj one", "adjacent", "uncommon"), topic("nn", "None one", "none", "common"), topic("mr", "Must one", "must_read", "epic"), topic("ab", "Adj two", "adjacent", "uncommon"), topic("rl", "Rel one", "relevant", "rare"), topic("ug", "Ungraded one", null, null)],
  };
  const railNames = () => [...document.querySelectorAll("#rail-topics .tprow .nm")].map((n) => n.textContent);

  it("go by grade, fold after two with the rest's badges and count, and hold still when a label regrades one", async () => {
    viewport(true);
    const calls = scriptFetch({
      "GET /api/status": status,
      "GET /api/topics": [],
      // labelling Must one none makes it a none
      "GET /api/papers/2610.00002": () => (calls.some((c) => c.method === "PUT") ? { ...many, topics: many.topics.map((t) => (t.topic.slug === "mr" ? { ...t, grade: "none", rarity: "common", label: { value: "none" } } : t)) } : many),
      "PUT /api/labels/mr/2610.00002": { saved: false },
    });
    renderApp("/paper/2610.00002");
    await screen.findByRole("heading", { name: "Epic one" });
    expect(railNames()).toEqual(["Must one", "Rel one"]);
    const fold = screen.getByRole("button", { name: /\+4 ·/ });
    expect(fold).toHaveTextContent("+4 · 2 adjacent, 1 none, 1 ungraded");
    expect(within(fold).getAllByTitle(/one|two/).map((b) => b.title)).toEqual(["Adj one", "Adj two", "None one", "Ungraded one"]);
    expect(fold).toHaveAttribute("aria-expanded", "false");
    await userEvent.click(fold);
    expect(railNames()).toEqual(["Must one", "Rel one", "Adj one", "Adj two", "None one", "Ungraded one"]);
    await userEvent.click(screen.getAllByRole("button", { name: "Label" })[0]!);
    await userEvent.click(within(screen.getByRole("group", { name: "Fit for Must one" })).getByRole("button", { name: "none" }));
    await waitFor(() => expect(document.querySelector('[data-topic="mr"] .gr')).toHaveTextContent("none"));
    expect(railNames()).toEqual(["Must one", "Rel one", "Adj one", "Adj two", "None one", "Ungraded one"]);
  });

  it("open from a note's Label when its topic is folded, with the row in view", async () => {
    viewport(true);
    const scrolled: string[] = [];
    const was = Element.prototype.scrollIntoView;
    Element.prototype.scrollIntoView = function (this: Element) {
      scrolled.push(this.getAttribute("data-topic") ?? "");
    };
    onTestFinished(() => void (Element.prototype.scrollIntoView = was));
    const split = { ...many, topics: many.topics.map((t) => (t.topic.slug === "nn" ? { ...t, state: "split" } : t)), states: [{ kind: "split", topic: "nn", revision: 1, error: null, pass_id: null }] };
    scriptFetch({ "GET /api/status": status, "GET /api/topics": [], "GET /api/papers/2610.00002": split });
    renderApp("/paper/2610.00002");
    await screen.findByRole("heading", { name: "Epic one" });
    expect(railNames()).toEqual(["Must one", "Rel one"]);
    await userEvent.click(within(screen.getByText(/Split on None one/).closest(".note") as HTMLElement).getByRole("button", { name: "Label" }));
    expect(railNames()).toContain("None one");
    expect(scrolled).toEqual(["nn"]);
  });

  it("show a grade from one lab as asking for a label, naming the side that refused", async () => {
    viewport(true);
    const verdict = { fit: "related", reason: "r", split: false, escalated: false, one_lab: true, opinions: { judge: { fit: "related", reason: "j" } } };
    const one = { ...many, topics: many.topics.map((t) => (t.topic.slug === "rl" ? { ...t, state: "one_lab", verdict } : t)), states: [{ kind: "one_lab", topic: "rl", revision: 1, error: null, pass_id: null }] };
    scriptFetch({ "GET /api/status": status, "GET /api/topics": [], "GET /api/papers/2610.00002": one });
    renderApp("/paper/2610.00002");
    await screen.findByRole("heading", { name: "Epic one" });
    expect(screen.getByText(/One lab on Rel one/).closest(".note")).toHaveTextContent("Check refused, judge related; related stands until it is labelled.");
    expect(document.querySelector('[data-topic="rl"]')).toHaveClass("ask");
    expect(document.querySelector('[data-topic="rl"] .vd')).toHaveTextContent("One lab · check refused · judge related · related stands");
  });

  it("keep an open Label open when a third topic arrives and the fold appears", async () => {
    viewport(true);
    const calls = scriptFetch({
      "GET /api/status": status,
      "GET /api/topics": [],
      "GET /api/papers/2610.00002": () => ({ ...many, topics: many.topics.slice(0, calls.some((c) => c.method === "PUT") ? 3 : 2) }),
      "PUT /api/labels/aa/2610.00002": { saved: false },
    });
    renderApp("/paper/2610.00002");
    await screen.findByRole("heading", { name: "Epic one" });
    await userEvent.click(screen.getAllByRole("button", { name: "Label" })[0]!);
    await userEvent.click(within(screen.getByRole("group", { name: "Fit for Adj one" })).getByRole("button", { name: "related" }));
    await screen.findByRole("button", { name: /\+1 ·/ });
    expect(screen.getByRole("group", { name: "Fit for Adj one" })).toBeInTheDocument();
  });

  it("have no fold with two rows", async () => {
    viewport(true);
    scriptFetch({ "GET /api/status": status, "GET /api/topics": [], "GET /api/papers/2610.00002": { ...many, topics: many.topics.slice(0, 2) } });
    renderApp("/paper/2610.00002");
    await screen.findByRole("heading", { name: "Epic one" });
    expect(railNames()).toEqual(["Adj one", "None one"]);
    expect(document.querySelector(".tpfold")).toBeNull();
  });

  it("go the same way in the phone's paper sheet", async () => {
    viewport(false);
    scriptFetch({ "GET /api/status": status, "GET /api/topics": [], "GET /api/papers/2610.00002": many });
    renderApp("/paper/2610.00002");
    await screen.findByRole("heading", { name: "Epic one" });
    await userEvent.click(screen.getByRole("button", { name: "More" }));
    const sheet = screen.getByRole("dialog", { name: "The paper" });
    const names = () => [...sheet.querySelectorAll(".slab .nm")].map((n) => n.textContent);
    expect(names()).toEqual(["Must one", "Rel one"]);
    const fold = within(sheet).getByRole("button", { name: /\+4 ·/ });
    expect(fold).toHaveTextContent("+4 · 2 adjacent, 1 none, 1 ungraded");
    await userEvent.click(fold);
    expect(names()).toEqual(["Must one", "Rel one", "Adj one", "Adj two", "None one", "Ungraded one"]);
  });

  it("say a topic grading again is ungraded on the phone, as the fold counts it", async () => {
    viewport(false);
    const again = { ...many, topics: many.topics.map((t) => (t.topic.slug === "rl" ? { ...t, state: "grading_again" } : t)) };
    scriptFetch({ "GET /api/status": status, "GET /api/topics": [], "GET /api/papers/2610.00002": again });
    renderApp("/paper/2610.00002");
    await screen.findByRole("heading", { name: "Epic one" });
    await userEvent.click(screen.getByRole("button", { name: "More" }));
    const sheet = screen.getByRole("dialog", { name: "The paper" });
    expect(within(sheet).getByRole("button", { name: /\+4 · 1 adjacent, 1 none, 2 ungraded/ })).toBeInTheDocument();
    await userEvent.click(within(sheet).getByRole("button", { name: /\+4 ·/ }));
    expect(within(sheet).getByText("ungraded · grading again")).toBeInTheDocument();
  });
});
