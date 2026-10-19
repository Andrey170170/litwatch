import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient } from "@tanstack/react-query";
import { describe, expect, it, vi } from "vitest";
import { qualityQuery } from "@/api/queries";
import { freshClient, json, renderApp, scriptFetch, viewport } from "@/test/harness";

const status = { state: "quiet", papers: 2, awaiting_classification: 0, analysis_failed: 0, ungraded: 0, fulltext_failed: 0, digests_failed: 0 };
const cls = { slug: "mi", name: "Mech interp", description: "Circuits and features.", parent: null, threshold: null };
const topic = { class: "mi", depth: "focus", monogram: "MI", colour: "#f3e600", status: "active", subject: "Circuits and features.", rubric: "central: it studies circuits", interests: "", keywords: ["circuit"] };
const tree = { classes: [cls], topics: [topic] };
const stats = { members: { mi: 3 }, thresholds: { mi: 0.5 }, topic_revisions: { mi: 2 } };
const split = {
  paper_id: "2610.00001",
  revision: 1,
  topic: "mi",
  depth: "focus",
  fit: "adjacent",
  reason: "r",
  split: true,
  escalated: false,
  one_lab: false,
  opinions: { judge: { fit: "adjacent", reason: "a" }, check: { fit: "none", reason: "b" } },
  recorded_at: "2026-10-14T03:00:00Z",
  title: "A split paper",
  label: null,
};
const stuck = { paper_id: "2610.00002", revision: 1, title: "A stuck paper", failed_runs: 3, last_error: "invalid", failed_at: "2026-10-14T03:00:00Z" };
const pass = { id: 7, paper_id: "2610.00003", revision: 1, title: "A textless paper", kind: "brief", state: "failed", failed_runs: 3, last_error: "the LaTeX did not build", queued_at: "2026-10-08T03:00:00Z", since: "2026-10-14T03:00:00Z" };
const digest = { id: 3, topic: "mi", period: "day", day: "2026-10-08", error: "the writer timed out twice", written_at: "2026-10-09T03:00:00Z" };
const quality = {
  night: { run: 6, error: null, splits: [split], awaiting_classification: [], analysis_failed: [stuck], ungraded: [], fulltext: [pass], digests: [digest] },
  cost: [{ provider: "anthropic", spent: 1.5, projected: 4, budget: 20, over: false }],
  recall: null,
  calibration: [],
  matters_no_topic: [],
  collected: {},
  reactions: [],
};
const badge = { slug: "mi", name: "Mech interp", depth: "focus", monogram: "MI", colour: "#f3e600" };
/** The shell's summary with `waiting` things on Needs you. */
const summaryOf = (waiting: number) => ({ status, topics: [badge], settings: { waiting, last_session: null, misses: 0, topics: 1, classes: 1, spent: 1.5, budget: 20, failing: [] } });
const common = {
  "GET /api/status": status,
  "GET /api/summary": summaryOf(4),
  "GET /api/topics": [{ depth: "focus", topics: [{ slug: "mi", name: "Mech interp", depth: "focus", monogram: "MI", colour: "#f3e600", papers: 3, saved: 0, counts: {}, newest_must_read: null }] }],
  "GET /api/settings/taxonomy": tree,
  "GET /api/settings/taxonomy/stats": stats,
  "GET /api/settings/quality": quality,
  "GET /api/settings/misses": [],
  "GET /api/settings/calibrate": [],
  "GET /api/settings/integrations": [],
};

describe("Settings", () => {
  it("lists the pages on the phone with what waits beside Needs you", async () => {
    viewport(false);
    scriptFetch(common);
    renderApp("/settings");
    const needs = await screen.findByRole("link", { name: /Needs you/ });
    await waitFor(() => expect(needs).toHaveTextContent("4 waiting"));
    expect(screen.getByRole("link", { name: /Topics/ })).toHaveTextContent("1 followed");
    expect(screen.getByRole("link", { name: /Runs and cost/ })).toHaveTextContent("$1.50 of $20.00 this month");
  });

  it("keeps the appearance on the device and puts it on the page at once", async () => {
    viewport(false);
    scriptFetch(common);
    renderApp("/settings");
    const group = await screen.findByRole("group", { name: "Appearance" });
    expect(within(group).getByRole("button", { name: "System" })).toHaveAttribute("aria-pressed", "true");
    fireEvent.click(within(group).getByRole("button", { name: "Dark" }));
    expect(document.documentElement.dataset.theme).toBe("dark");
    expect(localStorage.getItem("litwatch.appearance")).toBe("dark");
    expect(within(group).getByRole("button", { name: "Dark" })).toHaveAttribute("aria-pressed", "true");
    fireEvent.click(within(group).getByRole("button", { name: "System" }));
    expect(document.documentElement.dataset.theme).toBeUndefined();
    expect(localStorage.getItem("litwatch.appearance")).toBeNull();
  });

  it("settles a split in place: the label is recorded, the item keeps its spot and the count drops", async () => {
    viewport(true);
    const calls = scriptFetch({ ...common, "GET /api/summary": () => summaryOf(4 - calls.filter((c) => c.method === "PUT").length + calls.filter((c) => c.method === "DELETE").length), "PUT /api/labels/mi/2610.00001": { saved: false }, "DELETE /api/labels/mi/2610.00001": { saved: false } });
    renderApp("/settings/needs");
    expect(await screen.findByText("4 things wait on the owner. Each also shows on its paper's page.")).toBeInTheDocument();
    // The sidebar's count comes from the summary, refetched after the write; the node stays, its text changes.
    const count = await within(screen.getByRole("link", { name: /Needs you/ })).findByText("4");
    const item = screen.getByText("A split paper").closest(".need")!;
    expect(item).toHaveTextContent("adjacent stands");
    await userEvent.click(within(item as HTMLElement).getByRole("button", { name: "related" }));
    await waitFor(() => expect(calls.find((c) => c.method === "PUT")?.body).toEqual({ value: "related" }));
    await waitFor(() => expect(item).toHaveClass("settled"));
    expect(item).toHaveTextContent("labelled related");
    // Still first in its group, before the stuck paper.
    const titles = screen.getAllByRole("link", { name: /paper$/ }).map((l) => l.textContent);
    expect(titles).toEqual(["A split paper", "A stuck paper", "A textless paper"]);
    expect(screen.getByText("3 things wait on the owner. Each also shows on its paper's page.")).toBeInTheDocument();
    await waitFor(() => expect(count).toHaveTextContent("3"));
    expect(within(screen.getByRole("link", { name: /Needs you/ })).getByText("3")).toBe(count);
    // Tapping the label's segment again takes it back: the split waits once more, where it was.
    await userEvent.click(within(item as HTMLElement).getByRole("button", { name: "related" }));
    await waitFor(() => expect(calls.some((c) => c.method === "DELETE" && c.url === "/api/labels/mi/2610.00001")).toBe(true));
    await waitFor(() => expect(item).not.toHaveClass("settled"));
    expect(item).toHaveTextContent("adjacent stands");
    expect(screen.getByText("4 things wait on the owner. Each also shows on its paper's page.")).toBeInTheDocument();
  });

  it("keeps the visit's rows on the way back, refetches a fresh visit behind them, and never refetches the sidebar on the way", async () => {
    viewport(true);
    const renders: Record<string, number> = {};
    (globalThis as { __renders?: Record<string, number> }).__renders = renders;
    const paper = { id: "2610.00002", title: "A stuck paper", authors: ["A"], abstract: "A.", published: "2026-10-13", version: 1, revision: 1, arxiv_url: "", pdf_url: "", source_url: "", in_collection: true, saved: false, topics: [], states: [], publication_by_owner: false, card_requestable: false };
    // The second read of /quality finds the failure gone and the split too, and lands only when told to.
    let reads = 0;
    let land!: () => void;
    const later = { ...quality, night: { ...quality.night, splits: [], analysis_failed: [] } };
    const calls = scriptFetch({
      ...common,
      "GET /api/summary": () => summaryOf(4 - calls.filter((c) => c.method === "POST").length),
      "GET /api/settings/quality": () => (++reads > 1 ? new Promise<typeof later>((r) => (land = () => r(later))) : quality),
      "GET /api/settings/config": {},
      "GET /api/papers/2610.00002": paper,
      "POST /api/settings/analysis/2610.00002/retry": { saved: false },
    });
    // The app's client keeps a page's read while no page shows it (the harness's drops it at once).
    const { router } = renderApp("/settings/needs", new QueryClient({ defaultOptions: { queries: { retry: false } } }));
    const item = (await screen.findByRole("link", { name: "A stuck paper" })).closest(".need")!;
    const needs = screen.getByRole("link", { name: /Needs you/ });
    const count = await within(needs).findByText("4");
    const sideRenders = renders["settings-side"]!;
    await userEvent.click(within(item as HTMLElement).getByRole("button", { name: "Try again" }));
    await waitFor(() => expect(item).toHaveClass("settled"));
    const fetched = (url: string) => calls.filter((c) => c.url === url).length;
    expect(fetched("/api/settings/quality")).toBe(1);
    // The write refreshes the summary: the count changes in place, the node the same.
    await waitFor(() => expect(count).toHaveTextContent("3"));
    expect(fetched("/api/summary")).toBe(2);
    // To a sibling page and back: the settled row is still there, settled, with no fetch of this page's read.
    await userEvent.click(screen.getByRole("link", { name: /Configuration/ }));
    await waitFor(() => expect(document.querySelector("main .h1")).toHaveTextContent("Configuration"));
    router.history.back();
    expect((await screen.findByRole("link", { name: "A stuck paper" })).closest(".need")).toHaveClass("settled");
    expect(fetched("/api/settings/quality")).toBe(1);
    // By a link: a fresh visit refetches behind the cached rows, which show at once; "Updating…", never "Loading…".
    await userEvent.click(screen.getByRole("link", { name: /Configuration/ }));
    await waitFor(() => expect(document.querySelector("main .h1")).toHaveTextContent("Configuration"));
    await userEvent.click(needs);
    const again = (await screen.findByRole("link", { name: "A stuck paper" })).closest(".need")!;
    expect(again).toHaveClass("settled");
    expect(screen.queryByText("Loading…")).toBeNull();
    await screen.findByText("Updating…");
    expect(fetched("/api/settings/quality")).toBe(2);
    // To the paper and back before the read lands: still the cached rows, still updating; then they update in place.
    await userEvent.click(screen.getByRole("link", { name: "A stuck paper" }));
    await screen.findByRole("heading", { name: "A stuck paper" });
    router.history.back();
    expect((await screen.findByRole("link", { name: "A stuck paper" })).closest(".need")).toHaveClass("settled");
    expect(screen.queryByText("Loading…")).toBeNull();
    land();
    await waitFor(() => expect(screen.queryByRole("link", { name: "A stuck paper" })).toBeNull());
    expect(screen.getByText("2 things wait on the owner. Each also shows on its paper's page.")).toBeInTheDocument();
    expect(screen.queryByText("Updating…")).toBeNull();
    expect(fetched("/api/settings/quality")).toBe(2);
    // Through all of it the sidebar read the summary once more, after the write, and did not render again.
    expect(fetched("/api/summary")).toBe(2);
    expect(within(needs).getByText("3")).toBe(count);
    expect(renders["settings-side"]).toBe(sideRenders + 1);
  });

  it("lists a failed digest and writes it again, settling it in place with what came of it", async () => {
    viewport(true);
    const written = { id: 3, topic: { slug: "mi", name: "Mech interp", depth: "focus", monogram: "MI", colour: "#f3e600" }, period: "day", day: "2026-10-08", state: "written", counts: {}, gist: "g", text: "t", error: null, written_at: "2026-10-15T10:00:00Z", papers: [] };
    const calls = scriptFetch({ ...common, "POST /api/digests/3/write": written });
    renderApp("/settings/needs");
    expect(await screen.findByText("Digests · 1")).toBeInTheDocument();
    const item = screen.getByText("Daily digest · 8 Oct").closest(".need")!;
    expect(item).toHaveTextContent("Could not be written: the writer timed out twice");
    await userEvent.click(within(item as HTMLElement).getByRole("button", { name: "Write it again" }));
    await waitFor(() => expect(item).toHaveClass("settled"));
    expect(item).toHaveTextContent("Written again; it shows in the Digest.");
    expect(calls.filter((c) => c.method === "POST").map((c) => c.url)).toEqual(["/api/digests/3/write"]);
    expect(screen.getByText("3 things wait on the owner. Each also shows on its paper's page.")).toBeInTheDocument();
  });

  it("keeps a digest that failed again, or that the API would not write now, on the page with the reason", async () => {
    viewport(true);
    const failed = { id: 3, topic: { slug: "mi", name: "Mech interp", depth: "focus", monogram: "MI", colour: "#f3e600" }, period: "week", day: "2026-10-05", state: "failed", counts: {}, error: "the writer timed out again", written_at: "2026-10-15T10:00:00Z", papers: [] };
    const three = { ...quality, night: { ...quality.night, digests: [{ ...digest, period: "week", day: "2026-10-05" }, { ...digest, id: 4 }, { ...digest, id: 5, day: "2026-10-09" }] } };
    const calls = scriptFetch({
      ...common,
      "GET /api/settings/quality": three,
      "POST /api/digests/3/write": failed,
      "POST /api/digests/4/write": () => json({ detail: "another litwatch job is already running" }, 409),
      "POST /api/digests/5/write": () => json({ detail: "the digest is not one to write again" }, 409),
    });
    renderApp("/settings/needs");
    expect(await screen.findByText("6 things wait on the owner. Each also shows on its paper's page.")).toBeInTheDocument();
    const weekly = (await screen.findByText("Weekly digest · the week of 5 Oct")).closest(".need")!;
    await userEvent.click(within(weekly as HTMLElement).getByRole("button", { name: "Write it again" }));
    await waitFor(() => expect(weekly).toHaveTextContent("Failed again: the writer timed out again. The next run writes it again."));
    expect(weekly).not.toHaveClass("settled");
    expect(within(weekly as HTMLElement).getByRole("button", { name: "Write it again" })).toBeEnabled();
    const daily = screen.getByText("Daily digest · 8 Oct").closest(".need")!;
    await userEvent.click(within(daily as HTMLElement).getByRole("button", { name: "Write it again" }));
    await waitFor(() => expect(daily).toHaveTextContent("Not written now: another litwatch job is already running."));
    expect(within(daily as HTMLElement).getByRole("button", { name: "Write it again" })).toBeEnabled();
    // Both still wait, as the server still counts them. One the server no longer counts settles with its reason.
    expect(screen.getByText("6 things wait on the owner. Each also shows on its paper's page.")).toBeInTheDocument();
    const gone = screen.getByText("Daily digest · 9 Oct").closest(".need")!;
    await userEvent.click(within(gone as HTMLElement).getByRole("button", { name: "Write it again" }));
    await waitFor(() => expect(gone).toHaveClass("settled"));
    expect(gone).toHaveTextContent("No longer to be written: the digest is not one to write again.");
    expect(screen.getByText("5 things wait on the owner. Each also shows on its paper's page.")).toBeInTheDocument();
    // Each answer refreshed the summary: a write landed, or a 409 said the server's counts moved.
    await waitFor(() => expect(calls.filter((c) => c.url === "/api/summary").length).toBe(4));
  });

  it("names last night from the quality read, never a run of another kind", async () => {
    viewport(true);
    const manual = { id: 9, kind: "digests", started_at: "2026-10-15T12:00:00Z", finished_at: "2026-10-15T12:01:00Z", cost: 0.2, error: null, counts: { digests_written: 1 }, fetched: 0, inserted: 0, updated: 0 };
    const night = { ...manual, id: 6, kind: "nightly", started_at: "2026-10-14T03:00:00Z", counts: { classified: 40, graded: 12, digests_written: 2, digests_failed: 1 } };
    scriptFetch({ ...common, "GET /api/settings/quality": { ...quality, night: { ...quality.night, run: null } }, "GET /api/settings/runs": [manual] });
    const { unmount } = renderApp("/settings/overview");
    expect(await screen.findByText("No night yet")).toBeInTheDocument();
    expect(screen.queryByText(/Last night/)).toBeNull();
    unmount();
    scriptFetch({ ...common, "GET /api/settings/runs": [manual, night] });
    renderApp("/settings/overview");
    expect(await screen.findByText("Last night · 40 classified · 12 graded · 2 digests · $0.20")).toBeInTheDocument();
  });

  it("says what the refreshed card says after a skipped event", async () => {
    viewport(true);
    const failing = { event_id: 41, type: "paper.graded", paper_id: "2610.00001", failures: 3, retry_at: null, error: "502" };
    const card = (f: typeof failing | null, lag: number) => ({ name: "zotero", description: "The library", enabled: true, lag, failing: f, last_error: f?.error ?? null, skipped: f ? 0 : 1 });
    let reads = 0;
    scriptFetch({ ...common, "GET /api/settings/integrations": () => [card(++reads > 1 ? null : failing, 2)], "POST /api/settings/integrations/zotero/skip": { skipped: true } });
    renderApp("/settings/integrations");
    await userEvent.click(await screen.findByRole("button", { name: "Skip this event" }));
    expect(await screen.findByText("Event skipped · 2 events waiting")).toBeInTheDocument();
  });

  it("reads the cards again after a retry until the dispatcher has sent the event", async () => {
    viewport(true);
    const failing = { event_id: 41, type: "paper.graded", paper_id: "2610.00001", failures: 3, retry_at: null, error: "502" };
    const card = (f: typeof failing | null) => ({ name: "zotero", description: "The library", enabled: true, lag: 0, failing: f, last_error: f?.error ?? null, skipped: 0 });
    let reads = 0;
    // The first read after the retry still fails: the dispatcher has not run yet.
    scriptFetch({ ...common, "GET /api/settings/integrations": () => [card(++reads > 2 ? null : failing)], "POST /api/settings/integrations/zotero/retry": () => new Response(null, { status: 204 }) });
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      renderApp("/settings/integrations");
      await userEvent.click(await screen.findByRole("button", { name: "Retry now" }));
      await waitFor(() => expect(reads).toBe(2));
      expect(screen.getByRole("button", { name: "Retry now" })).toBeInTheDocument();
      await vi.advanceTimersByTimeAsync(3_000);
      await waitFor(() => expect(screen.queryByRole("button", { name: "Retry now" })).toBeNull());
    } finally {
      vi.useRealTimers();
    }
  });

  it("keeps a list as left on the way back from its subpage, and opens Overview afresh from /settings", async () => {
    viewport(true);
    const items = [{ position: 1, paper_id: "2610.00001", topic: "mi", why: "above", verdict: "related", answer: "related", answered_at: "2026-10-14T10:00:00Z", title: "Drawn 1", abstract: "A.", authors: ["A"], published: "2026-10-13", fit: {}, appraisal: null }];
    // The session's second read (a fresh visit) lands only when told to.
    const session = { id: 3, seed: 7, drawn_at: "2026-10-14T10:00:00Z", items };
    let sessionReads = 0;
    let land!: () => void;
    const calls = scriptFetch({
      ...common,
      "GET /api/settings/calibrate": [{ id: 3, seed: 7, drawn_at: "2026-10-14T10:00:00Z", papers: 1, answered: 1, differ: 0 }],
      "GET /api/settings/calibrate/3": () => (++sessionReads > 1 ? new Promise<typeof session>((r) => (land = () => r(session))) : session),
      "GET /api/settings/runs": [],
    });
    const { router } = renderApp("/settings/overview", new QueryClient({ defaultOptions: { queries: { retry: false } } }));
    await waitFor(() => expect(document.querySelector("main .h1")).toHaveTextContent("Overview"));
    const fetched = (url: string) => calls.filter((c) => c.url === url).length;
    await waitFor(() => expect(fetched("/api/settings/quality")).toBe(1));
    await userEvent.click(within(document.querySelector("aside.side")!).getByRole("link", { name: /Calibrate/ }));
    const row = await screen.findByRole("button", { name: /14 Oct/ });
    const listReads = fetched("/api/settings/calibrate"); // Overview's copy and Calibrate's own
    await userEvent.click(row);
    expect(await screen.findByText("Drawn 1")).toBeInTheDocument();
    router.history.back();
    await screen.findByRole("button", { name: /14 Oct/ });
    // The list was not refetched by the visit to its session.
    expect(fetched("/api/settings/calibrate")).toBe(listReads);
    expect(fetched("/api/settings/calibrate/3")).toBe(1);
    // The session again, by its row: a fresh visit shows the cached session at once and refetches behind it.
    await userEvent.click(screen.getByRole("button", { name: /14 Oct/ }));
    expect(await screen.findByText("Drawn 1")).toBeInTheDocument();
    expect(await screen.findByText("Updating…")).toBeInTheDocument();
    expect(fetched("/api/settings/calibrate/3")).toBe(2);
    land();
    await waitFor(() => expect(screen.queryByText("Updating…")).toBeNull());
    router.history.back();
    await screen.findByRole("button", { name: /14 Oct/ });
    // /settings opens Overview, by a redirect: a fresh visit all the same, refetched behind the tiles.
    await router.navigate({ to: "/settings" });
    await waitFor(() => expect(document.querySelector("main .h1")).toHaveTextContent("Overview"));
    await waitFor(() => expect(fetched("/api/settings/quality")).toBe(2));
    expect(screen.queryByText("Loading…")).toBeNull();
  });

  it("settles an item whose action answers 409 with the reason, in place", async () => {
    viewport(true);
    scriptFetch({ ...common, "POST /api/settings/fulltext/7/now": () => json({ detail: "the pass is not waiting for the owner" }, 409) });
    renderApp("/settings/needs");
    const item = (await screen.findByRole("link", { name: "A textless paper" })).closest(".need")!;
    await userEvent.click(within(item as HTMLElement).getByRole("button", { name: "Run it now" }));
    await waitFor(() => expect(item).toHaveClass("settled"));
    expect(item).toHaveTextContent("No longer waits on the owner: the pass is not waiting for the owner.");
    expect(screen.getByText("3 things wait on the owner. Each also shows on its paper's page.")).toBeInTheDocument();
  });

  it("starts Fix the text from the paper's text, so a title-only fix keeps the abstract", async () => {
    viewport(true);
    const garbled = { ...stuck, paper_id: "2610.00009", title: "A garbld title" };
    const calls = scriptFetch({
      ...common,
      "GET /api/settings/quality": { ...quality, night: { ...quality.night, awaiting_classification: [garbled] } },
      "GET /api/papers/2610.00009": { id: "2610.00009", title: "A garbld title", abstract: "The abstract as stored." },
      "POST /api/settings/classification/2610.00009/correction": () => new Response(null, { status: 204 }),
    });
    renderApp("/settings/needs");
    const item = (await screen.findByRole("link", { name: "A garbld title" })).closest(".need") as HTMLElement;
    await userEvent.click(within(item).getByRole("button", { name: "Fix the text" }));
    expect(await within(item).findByRole("textbox", { name: "Abstract" })).toHaveValue("The abstract as stored.");
    const save = within(item).getByRole("button", { name: "Save the text" });
    expect(save).toBeDisabled();
    const title = within(item).getByRole("textbox", { name: "Title" });
    await userEvent.clear(title);
    await userEvent.type(title, "A garbled title");
    await userEvent.click(save);
    await waitFor(() => expect(item).toHaveClass("settled"));
    expect(calls.find((c) => c.method === "POST")?.body).toEqual({ revision: 1, title: "A garbled title", abstract: "The abstract as stored." });
  });

  it("says when the paper's text could not be read, with the way to try again, and opens no form on it", async () => {
    viewport(true);
    const garbled = { ...stuck, paper_id: "2610.00009", title: "A garbld title" };
    let down = true;
    scriptFetch({
      ...common,
      "GET /api/settings/quality": { ...quality, night: { ...quality.night, awaiting_classification: [garbled] } },
      "GET /api/papers/2610.00009": () => (down ? json({ detail: "down" }, 500) : { id: "2610.00009", revision: 1, title: "A garbld title", abstract: "The abstract as stored." }),
    });
    renderApp("/settings/needs");
    const item = (await screen.findByRole("link", { name: "A garbld title" })).closest(".need") as HTMLElement;
    await userEvent.click(within(item).getByRole("button", { name: "Fix the text" }));
    expect(await within(item).findByText(/Could not load the paper's text/)).toBeInTheDocument();
    expect(within(item).queryByRole("textbox", { name: "Abstract" })).toBeNull();
    down = false;
    await userEvent.click(within(item).getByRole("button", { name: "Try again" }));
    expect(await within(item).findByRole("textbox", { name: "Abstract" })).toHaveValue("The abstract as stored.");
    expect(within(item).queryByText(/Could not load/)).toBeNull();
  });

  it("holds a Fix-the-text draft to the revision it was opened on: a rewrite meanwhile is said, and the text reloaded", async () => {
    viewport(true);
    const garbled = { ...stuck, paper_id: "2610.00009", title: "A garbld title" };
    let revision = 1;
    const calls = scriptFetch({
      ...common,
      "GET /api/settings/quality": () => ({ ...quality, night: { ...quality.night, awaiting_classification: [{ ...garbled, revision }] } }),
      "GET /api/papers/2610.00009": () => ({ id: "2610.00009", revision, title: "A garbld title", abstract: revision === 1 ? "The abstract as stored." : "The abstract, rewritten." }),
      "POST /api/settings/classification/2610.00009/correction": () => new Response(null, { status: 204 }),
    });
    const client = freshClient();
    renderApp("/settings/needs", client);
    const item = (await screen.findByRole("link", { name: "A garbld title" })).closest(".need") as HTMLElement;
    await userEvent.click(within(item).getByRole("button", { name: "Fix the text" }));
    expect(await within(item).findByRole("textbox", { name: "Abstract" })).toHaveValue("The abstract as stored.");
    const title = within(item).getByRole("textbox", { name: "Title" });
    await userEvent.clear(title);
    await userEvent.type(title, "A garbled title");
    // A refresh of the page brings the row on revision 2: the draft is for revision 1, and is not saved as 2.
    revision = 2;
    await client.invalidateQueries({ queryKey: qualityQuery("needs").queryKey });
    const notice = await within(item).findByRole("alert");
    expect(notice).toHaveTextContent("The paper's text changed meanwhile (revision 2; this draft is for revision 1).");
    expect(within(item).getByRole("button", { name: "Save the text" })).toBeDisabled();
    await userEvent.click(within(notice).getByRole("button", { name: "Reload the text" }));
    await waitFor(() => expect(within(item).queryByRole("alert")).toBeNull());
    expect(within(item).getByRole("textbox", { name: "Abstract" })).toHaveValue("The abstract, rewritten.");
    await userEvent.clear(within(item).getByRole("textbox", { name: "Title" }));
    await userEvent.type(within(item).getByRole("textbox", { name: "Title" }), "A garbled title");
    await userEvent.click(within(item).getByRole("button", { name: "Save the text" }));
    await waitFor(() => expect(calls.find((c) => c.method === "POST")?.body).toEqual({ revision: 2, title: "A garbled title", abstract: "The abstract, rewritten." }));
  });

  it("acknowledges a red group, then all: they fold away keeping their buttons, and the run status clears", async () => {
    viewport(true);
    const failed = { ...status, papers: 530, analysis_failed: 1, fulltext_failed: 1, digests_failed: 1 };
    const acks = () => calls.filter((c) => c.url === "/api/settings/acknowledge");
    const calls = scriptFetch({
      ...common,
      // What the server counts after each Acknowledge: the pass's group first, then everything.
      "GET /api/summary": () => ({ ...summaryOf(4 - [0, 1, 3][acks().length]!), status: [failed, { ...failed, fulltext_failed: 0 }, { ...status, papers: 530 }][acks().length] }),
      // It marks all it was named (`+00:00` as the API writes an instant).
      "POST /api/settings/acknowledge": (c) => c.body,
      "POST /api/settings/fulltext/7/now": () => new Response(null, { status: 204 }),
    });
    renderApp("/settings/needs");
    expect(await screen.findByRole("link", { name: "3 failed" })).toBeInTheDocument();
    const textGroup = screen.getByText("Full text · 1").closest(".ngrp") as HTMLElement;
    await userEvent.click(within(textGroup).getByRole("button", { name: "Acknowledge" }));
    await waitFor(() => expect(acks().map((c) => c.body)).toEqual([{ steps: [], passes: [{ id: 7, since: "2026-10-14T03:00:00Z" }], digests: [] }]));
    // The pass folds away; what remains keeps its order, and the run status counts one fewer.
    await waitFor(() => expect(screen.queryByRole("link", { name: "A textless paper" })).toBeNull());
    expect(screen.getAllByRole("link", { name: /paper$/ }).map((l) => l.textContent)).toEqual(["A split paper", "A stuck paper"]);
    expect(screen.getByRole("button", { name: "Acknowledged · 1" })).toHaveAttribute("aria-expanded", "false");
    expect(await screen.findByRole("link", { name: "2 failed" })).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Acknowledge all" }));
    // Splits are not acknowledged: they wait on a Fit.
    await waitFor(() => expect(acks()[1]?.body).toEqual({ steps: [{ step: "analysis", paper_id: "2610.00002", revision: 1, topic: null, failed_at: "2026-10-14T03:00:00Z" }], passes: [], digests: [3] }));
    expect(await screen.findByRole("link", { name: "530" })).toHaveClass("ok");
    expect(screen.queryByRole("button", { name: "Acknowledge all" })).toBeNull();
    expect(screen.getByText("1 thing waits on the owner. Each also shows on its paper's page.")).toBeInTheDocument();

    // Open, the section is grouped as above, and its rows keep their buttons.
    await userEvent.click(screen.getByRole("button", { name: "Acknowledged · 3" }));
    const folded = screen.getByRole("region", { name: "Acknowledged" });
    expect(within(folded).getAllByText(/ · 1$/).map((c) => c.textContent)).toEqual(["Analysis · 1", "Full text · 1", "Digests · 1"]);
    expect(within(folded).queryByRole("button", { name: "Acknowledge" })).toBeNull();
    const row = within(folded).getByRole("link", { name: "A textless paper" }).closest(".need") as HTMLElement;
    await userEvent.click(within(row).getByRole("button", { name: "Run it now" }));
    await waitFor(() => expect(row).toHaveClass("settled"));
  });

  it("folds the rows the API says were acknowledged, and counts only the others", async () => {
    viewport(true);
    const seen = "2026-10-19T08:00:00Z";
    const night = { ...quality.night, analysis_failed: [{ ...stuck, acknowledged_at: seen }], fulltext: [pass], digests: [{ ...digest, acknowledged_at: seen }] };
    // The pass stopped waiting as shown before the click landed: the API marks nothing.
    const calls = scriptFetch({ ...common, "GET /api/settings/quality": { ...quality, night }, "POST /api/settings/acknowledge": { steps: [], passes: [], digests: [] } });
    renderApp("/settings/needs");
    expect(await screen.findByRole("button", { name: "Acknowledged · 2" })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "A stuck paper" })).toBeNull();
    expect(screen.getByText("2 things wait on the owner. Each also shows on its paper's page.")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Acknowledge all" }));
    await waitFor(() => expect(calls.find((c) => c.method === "POST")?.body).toEqual({ steps: [], passes: [{ id: 7, since: "2026-10-14T03:00:00Z" }], digests: [] }));
    // So the pass stays where it was, still to acknowledge.
    await waitFor(() => expect(calls.filter((c) => c.url === "/api/summary").length).toBeGreaterThan(1));
    expect(screen.getByRole("link", { name: "A textless paper" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Acknowledged · 2" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Acknowledge all" })).toBeInTheDocument();
  });

  it("lists a grade from one lab in its own group, naming the side that refused, and says what every model refused, or what stopped it", async () => {
    viewport(true);
    const oneLab = { ...split, paper_id: "2610.00009", title: "A one-lab paper", fit: "central", split: false, one_lab: true, opinions: { check: { fit: "central", reason: "b" } } };
    const refused = { ...stuck, topic: "mi", refused: ["check", "judge"], stopped_by_refusal: true };
    // One side refused throughout while the other failed three runs: the runs stopped it.
    const halfRefused = { ...stuck, paper_id: "2610.00010", title: "A half-refused paper", topic: "mi", refused: ["judge"] };
    const night = { ...quality.night, splits: [split, oneLab], ungraded: [refused, halfRefused], fulltext: [{ ...pass, refused: true }] };
    const calls = scriptFetch({ ...common, "GET /api/settings/quality": { ...quality, night }, "PUT /api/labels/mi/2610.00009": { saved: true } });
    renderApp("/settings/needs");
    expect(await screen.findByText("Graded by one lab · 1")).toBeInTheDocument();
    expect(screen.getByText("Focus splits · 1")).toBeInTheDocument();
    const item = screen.getByText("A one-lab paper").closest(".need") as HTMLElement;
    expect(item).toHaveTextContent("judge refused · check central · central stands");
    const ungraded = screen.getByText("Refused by every check and judge model.").closest(".need") as HTMLElement;
    expect(within(ungraded).getByRole("button", { name: "Grade it now" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "A half-refused paper" }).closest(".need")).toHaveTextContent("judge refused · Grading failed 3 runs: invalid");
    expect(screen.getByRole("link", { name: "A textless paper" }).closest(".need")).toHaveTextContent("Brief refused by every model.");
    // Settled with the Fit control, as a split is.
    await userEvent.click(within(item).getByRole("button", { name: "related" }));
    await waitFor(() => expect(calls.find((c) => c.method === "PUT")?.body).toEqual({ value: "related" }));
    await waitFor(() => expect(item).toHaveClass("settled"));
  });

  it("lists a full-text pass that failed three runs and runs it now, settling it in place", async () => {
    viewport(true);
    const calls = scriptFetch({ ...common, "POST /api/settings/fulltext/7/now": () => new Response(null, { status: 204 }) });
    renderApp("/settings/needs");
    expect(await screen.findByText("Full text · 1")).toBeInTheDocument();
    const item = screen.getByRole("link", { name: "A textless paper" }).closest(".need")!;
    expect(item).toHaveTextContent("Brief failed 3 runs: the LaTeX did not build");
    await userEvent.click(within(item as HTMLElement).getByRole("button", { name: "Run it now" }));
    await waitFor(() => expect(calls.some((c) => c.method === "POST" && c.url === "/api/settings/fulltext/7/now")).toBe(true));
    await waitFor(() => expect(item).toHaveClass("settled"));
    expect(item).toHaveTextContent("Running now, first in line, with three more runs.");
    expect(within(item as HTMLElement).queryByRole("button", { name: "Run it now" })).toBeNull();
  });

  it("runs a waiting full-text pass on the paper's PDF, settling it in place", async () => {
    viewport(true);
    const calls = scriptFetch({ ...common, "POST /api/settings/fulltext/7/pdf": () => new Response(null, { status: 204 }) });
    renderApp("/settings/needs");
    expect(await screen.findByText("Full text · 1")).toBeInTheDocument();
    const item = screen.getByRole("link", { name: "A textless paper" }).closest(".need")! as HTMLElement;
    await userEvent.click(within(item).getByRole("button", { name: "Use the PDF" }));
    await waitFor(() => expect(item).toHaveClass("settled"));
    expect(calls.filter((c) => c.method === "POST").map((c) => c.url)).toEqual(["/api/settings/fulltext/7/pdf"]);
    expect(item).toHaveTextContent("Running now, on the PDF, first in line, with three more runs.");
    expect(within(item).queryByRole("button", { name: "Use the PDF" })).toBeNull();
  });

  it("follows a class with its generated defaults once a Focus topic has a rubric", async () => {
    viewport(true);
    scriptFetch({ ...common, "GET /api/settings/taxonomy": { ...tree, topics: [] } });
    renderApp("/settings/topics/mi");
    const follow = await screen.findByRole("button", { name: "Follow" });
    expect(follow).toBeDisabled();
    expect(follow).toHaveAttribute("title", expect.stringContaining("a Focus topic a rubric"));
    await userEvent.type(screen.getByRole("textbox", { name: "Rubric" }), "Central: finds a circuit.");
    expect(follow).toBeEnabled();
  });

  it("offers Follow only once the subject would pass: a short generated one is not followed as it is", async () => {
    viewport(true);
    scriptFetch({ ...common, "GET /api/settings/taxonomy": { classes: [{ ...cls, description: "Circuits." }], topics: [] } });
    renderApp("/settings/topics/mi");
    const follow = await screen.findByRole("button", { name: "Follow" });
    await userEvent.type(screen.getByRole("textbox", { name: "Rubric" }), "Central: finds a circuit.");
    expect(follow).toBeDisabled();
    expect(follow).toHaveAttribute("title", expect.stringContaining("20 characters"));
    await userEvent.type(screen.getByLabelText(/Subject/), " Circuits and features in transformers.");
    expect(follow).toBeEnabled();
    await userEvent.clear(screen.getByRole("textbox", { name: "Rubric" }));
    expect(follow).toBeDisabled();
  });

  it("names a stuck Deep pass as a Deep card", async () => {
    viewport(true);
    const deep = { ...pass, id: 8, kind: "deep", title: "A carded paper" };
    scriptFetch({ ...common, "GET /api/settings/quality": { ...quality, night: { ...quality.night, fulltext: [deep] } } });
    renderApp("/settings/needs");
    const item = (await screen.findByRole("link", { name: "A carded paper" })).closest(".need")!;
    expect(item).toHaveTextContent("Deep card failed 3 runs");
  });

  it("saves a topic only once something changed, then offers the re-run in place of Save", async () => {
    viewport(true);
    const after = { ...topic, interests: "Sparse circuits" };
    const calls = scriptFetch({
      ...common,
      "GET /api/settings/taxonomy": () => (calls.some((c) => c.method === "PUT") ? { ...tree, topics: [after] } : tree),
      "GET /api/settings/topics/mi/keywords": { circuit: 4 },
      "PUT /api/settings/topics/mi": after,
    });
    renderApp("/settings/topics/mi");
    const save = await screen.findByRole("button", { name: "Save" });
    expect(save).toBeDisabled();
    await userEvent.type(screen.getByRole("textbox", { name: "Interests" }), "Sparse circuits");
    expect(save).toBeEnabled();
    await userEvent.click(save);
    await waitFor(() => expect(calls.find((c) => c.method === "PUT")?.body).toMatchObject({ class: "mi", interests: "Sparse circuits", keywords: ["circuit"] }));
    expect(await screen.findByText("Saved as version 2. It applies to papers classified or graded from now on.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Save" })).toBeNull();
    // The taxonomy's refetch after the save does not reopen the form; the keyword counts are fetched again.
    await waitFor(() => expect(calls.filter((c) => c.url === "/api/settings/taxonomy").length).toBeGreaterThan(1));
    expect(screen.getByText("Saved as version 2. It applies to papers classified or graded from now on.")).toBeInTheDocument();
    await waitFor(() => expect(calls.filter((c) => c.url === "/api/settings/topics/mi/keywords").length).toBeGreaterThan(1));
    // An edit after the save brings Save back in the offer's place; the offer never describes what was not sent.
    await userEvent.type(screen.getByRole("textbox", { name: "Interests" }), " and more");
    expect(screen.queryByText(/Saved as version/)).toBeNull();
    expect(screen.getByRole("button", { name: "Save" })).toBeEnabled();
  });

  it("records a Calibrate answer on Next, then shows the next paper", async () => {
    viewport(true);
    const items = [1, 2].map((position) => ({ position, paper_id: `2610.0000${position}`, topic: "mi", why: "above", verdict: "related", answer: null, answered_at: null, title: `Drawn ${position}`, abstract: "A.", authors: ["A"], published: "2026-10-13", fit: {}, appraisal: null }));
    const calls = scriptFetch({ ...common, "GET /api/settings/config": {}, "GET /api/settings/calibrate/1": { id: 1, seed: 7, drawn_at: "2026-10-15T10:00:00Z", items }, "PUT /api/settings/calibrate/1/items/1": { recorded: true } });
    // The app's client keeps a page's read while no page shows it (the harness's drops it at once).
    const { router } = renderApp("/settings/calibrate/1", new QueryClient({ defaultOptions: { queries: { retry: false } } }));
    expect(await screen.findByText("Drawn 1")).toBeInTheDocument();
    await screen.findByRole("group", { name: "Fit for Mech interp" });
    await userEvent.click(within(screen.getByRole("group", { name: "Fit for Mech interp" })).getByRole("button", { name: "related" }));
    await userEvent.click(screen.getByRole("button", { name: "matters" }));
    await userEvent.click(screen.getByRole("button", { name: "Next ›" }));
    await waitFor(() => expect(calls.find((c) => c.method === "PUT")?.body).toEqual({ fit: { mi: "related" }, value: "matters", note: null }));
    expect(await screen.findByText("Drawn 2")).toBeInTheDocument();
    expect(router.state.location.href).toBe("/settings/calibrate/1?at=2");
    expect(router.history.length).toBe(1); // a move within the session replaces the entry: Back leaves the session
    // The move to the next paper is no visit: away and back, the session is not read again (the write read it).
    const reads = () => calls.filter((c) => c.url === "/api/settings/calibrate/1").length;
    await waitFor(() => expect(reads()).toBe(2));
    await userEvent.click(screen.getByRole("link", { name: /Configuration/ }));
    await waitFor(() => expect(document.querySelector("main .h1")).toHaveTextContent("Configuration"));
    router.history.back();
    expect(await screen.findByText("Drawn 2")).toBeInTheDocument();
    expect(reads()).toBe(2);
  });

  it("keeps a label given since the session when only the appraisal is edited: the control shows it, the answer sends no Fit", async () => {
    viewport(true);
    const item = { position: 1, paper_id: "2610.00001", topic: "mi", why: "above", verdict: "related", answer: "related", answered_at: "2026-10-14T10:00:00Z", title: "Drawn 1", abstract: "A.", authors: ["A"], published: "2026-10-13", fit: { mi: "none" }, appraisal: null };
    const calls = scriptFetch({ ...common, "GET /api/settings/calibrate/1": { id: 1, seed: 7, drawn_at: "2026-10-15T10:00:00Z", items: [item] }, "PUT /api/settings/calibrate/1/items/1": () => new Response(null, { status: 204 }) });
    renderApp("/settings/calibrate/1?at=1");
    expect(await screen.findByText("Drawn 1")).toBeInTheDocument();
    const fit = await screen.findByRole("group", { name: "Fit for Mech interp" });
    expect(within(fit).getByRole("button", { name: "none" })).toHaveAttribute("aria-pressed", "true");
    await userEvent.click(screen.getByRole("button", { name: "matters" }));
    await userEvent.click(screen.getByRole("button", { name: /Done|Next/ }));
    await waitFor(() => expect(calls.find((c) => c.method === "PUT")?.body).toEqual({ fit: {}, value: "matters", note: null }));
  });

  it("records what was changed before the strip moves to another paper", async () => {
    viewport(true);
    const items = [1, 2].map((position) => ({ position, paper_id: `2610.0000${position}`, topic: "mi", why: "above", verdict: "related", answer: null, answered_at: null, title: `Drawn ${position}`, abstract: "A.", authors: ["A"], published: "2026-10-13", fit: {}, appraisal: null }));
    const calls = scriptFetch({ ...common, "GET /api/settings/calibrate/1": { id: 1, seed: 7, drawn_at: "2026-10-15T10:00:00Z", items }, "PUT /api/settings/calibrate/1/items/1": { recorded: true } });
    renderApp("/settings/calibrate/1?at=1");
    await screen.findByRole("group", { name: "Fit for Mech interp" });
    await userEvent.click(within(screen.getByRole("group", { name: "Fit for Mech interp" })).getByRole("button", { name: "related" }));
    await userEvent.click(screen.getByRole("tab", { name: "Paper 2" }));
    await waitFor(() => expect(calls.find((c) => c.method === "PUT")?.body).toEqual({ fit: { mi: "related" }, value: null, note: null }));
    expect(await screen.findByText("Drawn 2")).toBeInTheDocument();
  });

  // -- Calibrate's draft: the item as last read, plus what was changed on it ---------------------------------------

  const drawnItem = (position: number, more: object = {}) => ({ position, paper_id: `2610.0000${position}`, topic: "mi", why: "above", verdict: "related", answer: null, answered_at: null, title: `Drawn ${position}`, abstract: "A.", authors: ["A"], published: "2026-10-13", fit: {}, appraisal: null, ...more });
  const fitGroup = () => screen.getByRole("group", { name: "Fit for Mech interp" });
  const fitButton = (name: string) => within(fitGroup()).getByRole("button", { name });

  it("follows a refreshed label behind an untouched Fit, keeps an edited one, and sends no Fit for an appraisal edit", async () => {
    viewport(true);
    // Both papers answered related before; a read since finds the owner's newer word, none, on each.
    const before = [drawnItem(1, { answer: "related", answered_at: "2026-10-14T10:00:00Z", fit: { mi: "related" } }), drawnItem(2, { answer: "related", answered_at: "2026-10-14T10:00:00Z", fit: { mi: "related" } })];
    const after = before.map((it) => ({ ...it, fit: { mi: "none" } }));
    let reads = 0;
    const calls = scriptFetch({ ...common, "GET /api/settings/calibrate/1": () => ({ id: 1, seed: 7, drawn_at: "2026-10-15T10:00:00Z", items: ++reads > 1 ? after : before }), "PUT /api/settings/calibrate/1/items/1": () => new Response(null, { status: 204 }), "PUT /api/settings/calibrate/1/items/2": () => new Response(null, { status: 204 }) });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    renderApp("/settings/calibrate/1?at=1", client);
    expect(await screen.findByText("Drawn 1")).toBeInTheDocument();
    await screen.findByRole("group", { name: "Fit for Mech interp" });
    expect(fitButton("related")).toHaveAttribute("aria-pressed", "true");
    // The refresh a fresh visit makes: the untouched control follows the newer label.
    await client.invalidateQueries({ queryKey: ["settings", "calibrate/1"] });
    await waitFor(() => expect(fitButton("none")).toHaveAttribute("aria-pressed", "true"));
    await userEvent.click(screen.getByRole("button", { name: "matters" }));
    await userEvent.click(screen.getByRole("button", { name: "Next ›" }));
    await waitFor(() => expect(calls.find((c) => c.url === "/api/settings/calibrate/1/items/1")?.body).toEqual({ fit: {}, value: "matters", note: null }));
    // On the next paper, a Fit edited before the refresh lands stands over it; the answer sends that Fit.
    expect(await screen.findByText("Drawn 2")).toBeInTheDocument();
    await waitFor(() => expect(fitButton("none")).toHaveAttribute("aria-pressed", "true"));
    await userEvent.click(fitButton("central"));
    await client.invalidateQueries({ queryKey: ["settings", "calibrate/1"] });
    await waitFor(() => expect(reads).toBeGreaterThan(3));
    expect(fitButton("central")).toHaveAttribute("aria-pressed", "true");
    await userEvent.click(screen.getByRole("button", { name: "Done" }));
    await waitFor(() => expect(calls.find((c) => c.url === "/api/settings/calibrate/1/items/2")?.body).toEqual({ fit: { mi: "central" }, value: null, note: null }));
  });

  it("shows a recorded answer faint where its label was taken back, and writes it again only when chosen again", async () => {
    viewport(true);
    const items = [drawnItem(1, { answer: "related", answered_at: "2026-10-14T10:00:00Z" }), drawnItem(2, { answer: "related", answered_at: "2026-10-14T10:00:00Z" })];
    const calls = scriptFetch({ ...common, "GET /api/settings/calibrate/1": { id: 1, seed: 7, drawn_at: "2026-10-15T10:00:00Z", items }, "PUT /api/settings/calibrate/1/items/1": () => new Response(null, { status: 204 }), "PUT /api/settings/calibrate/1/items/2": () => new Response(null, { status: 204 }) });
    renderApp("/settings/calibrate/1?at=1");
    expect(await screen.findByText("Drawn 1")).toBeInTheDocument();
    await screen.findByRole("group", { name: "Fit for Mech interp" });
    expect(fitButton("related")).toHaveClass("auto");
    expect(fitButton("related")).toHaveAttribute("aria-pressed", "false");
    expect(screen.getByText(/answered related before, no label stands/)).toBeInTheDocument();
    // Chosen, then tapped again: the edit is taken back and the answer stays history.
    await userEvent.click(fitButton("related"));
    expect(fitButton("related")).toHaveAttribute("aria-pressed", "true");
    await userEvent.click(fitButton("related"));
    expect(fitButton("related")).toHaveAttribute("aria-pressed", "false");
    await userEvent.type(screen.getByRole("textbox", { name: "Note" }), "a note");
    await userEvent.click(screen.getByRole("button", { name: "Next ›" }));
    await waitFor(() => expect(calls.find((c) => c.url === "/api/settings/calibrate/1/items/1")?.body).toEqual({ fit: {}, value: null, note: "a note" }));
    // Chosen again on the next paper, it is a label once more.
    expect(await screen.findByText("Drawn 2")).toBeInTheDocument();
    await userEvent.click(fitButton("related"));
    await userEvent.click(screen.getByRole("button", { name: "Done" }));
    await waitFor(() => expect(calls.find((c) => c.url === "/api/settings/calibrate/1/items/2")?.body).toEqual({ fit: { mi: "related" }, value: null, note: null }));
  });

  it("locks the editor and its navigation while an answer is saved, so nothing typed meanwhile is lost", async () => {
    viewport(true);
    let release!: () => void;
    const calls = scriptFetch({ ...common, "GET /api/settings/calibrate/1": { id: 1, seed: 7, drawn_at: "2026-10-15T10:00:00Z", items: [drawnItem(1), drawnItem(2)] }, "PUT /api/settings/calibrate/1/items/2": () => new Promise<Response>((r) => (release = () => r(new Response(null, { status: 204 })))) });
    const { router } = renderApp("/settings/calibrate/1?at=2");
    expect(await screen.findByText("Drawn 2")).toBeInTheDocument();
    await screen.findByRole("group", { name: "Fit for Mech interp" });
    await userEvent.click(fitButton("related"));
    await userEvent.click(screen.getByRole("button", { name: "Done" }));
    await waitFor(() => expect(calls.filter((c) => c.method === "PUT").length).toBe(1));
    expect(screen.getByRole("button", { name: "Saving…" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "‹ Back" })).toBeDisabled();
    expect(fitButton("central")).toBeDisabled();
    expect(screen.getByRole("button", { name: "matters" })).toBeDisabled();
    expect(screen.getByRole("textbox", { name: "Note" })).toBeDisabled();
    expect(screen.getByRole("tab", { name: "Paper 1" })).toBeDisabled();
    await userEvent.click(screen.getByRole("tab", { name: "Paper 1" }));
    expect(screen.getByText("Drawn 2")).toBeInTheDocument();
    release();
    await waitFor(() => expect(router.state.location.href).toMatch(/review=/));
    expect(calls.filter((c) => c.method === "PUT").length).toBe(1);
  });

  it("gives the phone's controls back with the draft kept when a save fails", async () => {
    viewport(false);
    const calls = scriptFetch({ ...common, "GET /api/settings/calibrate/1": { id: 1, seed: 7, drawn_at: "2026-10-15T10:00:00Z", items: [drawnItem(1), drawnItem(2)] }, "PUT /api/settings/calibrate/1/items/1": () => json({ detail: "the database is away" }, 500) });
    const { router } = renderApp("/settings/calibrate/1?at=1");
    expect(await screen.findByText("Drawn 1")).toBeInTheDocument();
    await screen.findByRole("group", { name: "Fit for Mech interp" });
    await userEvent.click(fitButton("related"));
    await userEvent.type(screen.getByRole("textbox", { name: "Note" }), "kept");
    await userEvent.click(screen.getByRole("button", { name: "Next ›" }));
    await waitFor(() => expect(calls.filter((c) => c.method === "PUT").length).toBe(1));
    await waitFor(() => expect(screen.getByRole("button", { name: "Next ›" })).toBeEnabled());
    expect(router.state.location.href).toBe("/settings/calibrate/1?at=1");
    expect(fitButton("related")).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("textbox", { name: "Note" })).toHaveValue("kept");
    expect(screen.getByRole("button", { name: "Go to a paper" })).toBeEnabled();
  });

  it("asks for the drawn topic's Fit before moving on with edits, and still skips an untouched paper", async () => {
    viewport(true);
    const calls = scriptFetch({ ...common, "GET /api/settings/calibrate/1": { id: 1, seed: 7, drawn_at: "2026-10-15T10:00:00Z", items: [drawnItem(1), drawnItem(2)] }, "PUT /api/settings/calibrate/1/items/1": () => new Response(null, { status: 204 }) });
    const { router } = renderApp("/settings/calibrate/1?at=1");
    expect(await screen.findByText("Drawn 1")).toBeInTheDocument();
    await screen.findByRole("group", { name: "Fit for Mech interp" });
    await userEvent.click(screen.getByRole("button", { name: "matters" }));
    await userEvent.click(screen.getByRole("button", { name: "Next ›" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Choose the Fit for Mech interp to record this paper");
    expect(calls.filter((c) => c.method === "PUT")).toEqual([]);
    expect(screen.getByText("Drawn 1")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "matters" })).toHaveAttribute("aria-pressed", "true");
    // The strip asks too: the edits would be lost otherwise.
    await userEvent.click(screen.getByRole("tab", { name: "Paper 2" }));
    expect(screen.getByText("Drawn 1")).toBeInTheDocument();
    await userEvent.click(fitButton("related"));
    expect(screen.queryByRole("alert")).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Next ›" }));
    await waitFor(() => expect(calls.find((c) => c.method === "PUT")?.body).toEqual({ fit: { mi: "related" }, value: "matters", note: null }));
    // An untouched paper is left as it was: Done moves to the review with no write.
    expect(await screen.findByText("Drawn 2")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Done" }));
    await waitFor(() => expect(router.state.location.href).toMatch(/review=/));
    expect(calls.filter((c) => c.method === "PUT").length).toBe(1);
  });

  // -- settlement from the mutation, whatever page is shown when the write returns ----------------------------------

  it("settles rows whose writes return after Needs you was left, and does not pull the reader back", async () => {
    viewport(true);
    let writeDigest!: () => void;
    let retryAnalysis!: () => void;
    const written = { id: 3, topic: badge, period: "day", day: "2026-10-08", state: "written", counts: {}, gist: "g", text: "t", error: null, written_at: "2026-10-15T10:00:00Z", papers: [] };
    scriptFetch({
      ...common,
      "GET /api/settings/config": {},
      "POST /api/digests/3/write": () => new Promise<Response>((r) => (writeDigest = () => r(json(written)))),
      // The stuck paper was never read: nothing cached to reconcile from, the row settles by name all the same.
      "POST /api/settings/analysis/2610.00002/retry": () => new Promise<Response>((r) => (retryAnalysis = () => r(new Response(null, { status: 204 })))),
    });
    const { router } = renderApp("/settings/needs", new QueryClient({ defaultOptions: { queries: { retry: false } } }));
    const digestRow = (await screen.findByText("Daily digest · 8 Oct")).closest(".need") as HTMLElement;
    await userEvent.click(within(digestRow).getByRole("button", { name: "Write it again" }));
    const stuckRow = screen.getByRole("link", { name: "A stuck paper" }).closest(".need") as HTMLElement;
    await userEvent.click(within(stuckRow).getByRole("button", { name: "Try again" }));
    await userEvent.click(screen.getByRole("link", { name: /Configuration/ }));
    await waitFor(() => expect(document.querySelector("main .h1")).toHaveTextContent("Configuration"));
    writeDigest();
    retryAnalysis();
    await waitFor(() => expect(within(screen.getByRole("link", { name: /Needs you/ })).getByText("4")).toBeInTheDocument());
    // Still on Configuration: the writes landed, nobody was moved.
    expect(document.querySelector("main .h1")).toHaveTextContent("Configuration");
    router.history.back();
    const again = (await screen.findByText("Daily digest · 8 Oct")).closest(".need")!;
    await waitFor(() => expect(again).toHaveClass("settled"));
    expect(again).toHaveTextContent("Written again; it shows in the Digest.");
    const stuckAgain = screen.getByRole("link", { name: "A stuck paper" }).closest(".need")!;
    expect(stuckAgain).toHaveClass("settled");
    expect(stuckAgain).toHaveTextContent("Analysis gets three more runs, from the next one.");
    expect(screen.getByText("2 things wait on the owner. Each also shows on its paper's page.")).toBeInTheDocument();
  });

  it("settles an ungraded paper whose grading failed again, as it no longer waits on the owner", async () => {
    viewport(true);
    const ungraded = { ...stuck, paper_id: "2610.00005", title: "An ungraded paper", topic: "mi" };
    scriptFetch({ ...common, "GET /api/settings/quality": { ...quality, night: { ...quality.night, ungraded: [ungraded] } }, "POST /api/settings/grading/mi/2610.00005/now": { graded: false, fit: null, error: "no verdict came back" } });
    renderApp("/settings/needs");
    const item = (await screen.findByRole("link", { name: "An ungraded paper" })).closest(".need") as HTMLElement;
    await userEvent.click(within(item).getByRole("button", { name: "Grade it now" }));
    await waitFor(() => expect(item).toHaveClass("settled"));
    expect(item).toHaveTextContent("Grading failed again: no verdict came back. The nightly runs try twice more.");
    expect(screen.getByText("4 things wait on the owner. Each also shows on its paper's page.")).toBeInTheDocument();
  });

  it("keeps a settlement when an older read of the page lands after it, rows where they were", async () => {
    viewport(true);
    let reads = 0;
    let land!: () => void;
    const calls = scriptFetch({
      ...common,
      "GET /api/settings/config": {},
      // The fresh visit's read is held; it carries the split still unlabelled, as read before the write.
      "GET /api/settings/quality": () => (++reads > 1 ? new Promise<typeof quality>((r) => (land = () => r(quality))) : quality),
      "PUT /api/labels/mi/2610.00001": { saved: false },
      "DELETE /api/labels/mi/2610.00001": { saved: false },
    });
    renderApp("/settings/needs", new QueryClient({ defaultOptions: { queries: { retry: false } } }));
    await screen.findByText("A split paper");
    // Away and back by the link: a fresh visit, its read held behind the cached rows.
    await userEvent.click(screen.getByRole("link", { name: /Configuration/ }));
    await waitFor(() => expect(document.querySelector("main .h1")).toHaveTextContent("Configuration"));
    await userEvent.click(screen.getByRole("link", { name: /Needs you/ }));
    await screen.findByText("Updating…");
    const item = (await screen.findByText("A split paper")).closest(".need") as HTMLElement;
    await userEvent.click(within(item).getByRole("button", { name: "related" }));
    await waitFor(() => expect(item).toHaveClass("settled"));
    expect(screen.getByText("3 things wait on the owner. Each also shows on its paper's page.")).toBeInTheDocument();
    // The old read lands: nothing moves, the settlement stands, the count stays, and the page stops updating.
    land();
    await waitFor(() => expect(screen.queryByText("Updating…")).toBeNull());
    expect((screen.getByText("A split paper").closest(".need") as HTMLElement)).toHaveClass("settled");
    expect(screen.getByText("3 things wait on the owner. Each also shows on its paper's page.")).toBeInTheDocument();
    expect(screen.getAllByRole("link", { name: /paper$/ }).map((l) => l.textContent)).toEqual(["A split paper", "A stuck paper", "A textless paper"]);
    expect(calls.filter((c) => c.url === "/api/settings/quality").length).toBe(2);
    // Taken back from the mutation too: the split waits again.
    await userEvent.click(within(item).getByRole("button", { name: "related" }));
    await waitFor(() => expect(item).not.toHaveClass("settled"));
    expect(screen.getByText("4 things wait on the owner. Each also shows on its paper's page.")).toBeInTheDocument();
  });

  it("patches the session's row in the past-session lists, Calibrate's and Overview's, from answers and More", async () => {
    viewport(true);
    const items = [drawnItem(1)];
    const sessionNow = () => ({ id: 3, seed: 7, drawn_at: "2026-10-14T10:00:00Z", items });
    const calls = scriptFetch({
      ...common,
      "GET /api/settings/runs": [],
      "GET /api/settings/calibrate": [{ id: 3, seed: 7, drawn_at: "2026-10-14T10:00:00Z", papers: 1, answered: 0, differ: 0 }],
      "GET /api/settings/calibrate/3": sessionNow,
      "PUT /api/settings/calibrate/3/items/1": () => {
        items[0] = drawnItem(1, { answer: "none", answered_at: "2026-10-15T10:00:00Z", fit: { mi: "none" } });
        return new Response(null, { status: 204 });
      },
      "POST /api/settings/calibrate/3/more": () => {
        items.push(drawnItem(items.length + 1, { why: "split" }));
        return { added: 1 };
      },
    });
    const { router } = renderApp("/settings/overview", new QueryClient({ defaultOptions: { queries: { retry: false } } }));
    expect(await screen.findByText("Last Calibrate session · 0 of 1 answered")).toBeInTheDocument();
    await userEvent.click(within(document.querySelector("aside.side")!).getByRole("link", { name: /Calibrate/ }));
    const row = await screen.findByRole("button", { name: /14 Oct/ });
    expect(row).toHaveTextContent("0 of 1 answered · 0 differ · seed 7unfinished");
    const listReads = calls.filter((c) => c.url === "/api/settings/calibrate").length;
    await userEvent.click(row);
    // A past session opens on its review; Continue goes to its first unanswered paper.
    await userEvent.click(await screen.findByRole("button", { name: "Continue · 1 left" }));
    await screen.findByText("Drawn 1");
    await screen.findByRole("group", { name: "Fit for Mech interp" });
    await userEvent.click(fitButton("none"));
    await userEvent.click(screen.getByRole("button", { name: "Done" }));
    await waitFor(() => expect(router.state.location.href).toMatch(/review=/));
    const more = await screen.findByRole("button", { name: "Add the papers waiting for a word" });
    await userEvent.click(more);
    await waitFor(() => expect(screen.getAllByRole("link", { name: /^Drawn/ }).length).toBe(2));
    await userEvent.click(more);
    await waitFor(() => expect(screen.getAllByRole("link", { name: /^Drawn/ }).length).toBe(3));
    // Back to the review Continue left, then to the list: the row reads the counts as they are now, not refetched.
    router.history.back();
    await waitFor(() => expect(router.state.location.href).toBe("/settings/calibrate/3?review=1"));
    router.history.back();
    const back = await screen.findByRole("button", { name: /14 Oct/ });
    expect(back).toHaveTextContent("1 of 3 answered · 1 differ · seed 7unfinished");
    expect(calls.filter((c) => c.url === "/api/settings/calibrate").length).toBe(listReads);
    router.history.back();
    expect(await screen.findByText("Last Calibrate session · 1 of 3 answered")).toBeInTheDocument();
    expect(calls.filter((c) => c.url === "/api/settings/calibrate").length).toBe(listReads);
  });

  // -- Overview, Topics -----------------------------------------------------------------------------------------------

  it("marks last night red when some of its steps failed, as Runs and cost does", async () => {
    viewport(true);
    const night = { id: 6, kind: "nightly", started_at: "2026-10-14T03:00:00Z", finished_at: "2026-10-14T04:00:00Z", cost: 0.2, error: null, counts: { classified: 40, graded: 12, grading_failed: 20 }, fetched: 0, inserted: 0, updated: 0 };
    scriptFetch({ ...common, "GET /api/settings/runs": [night] });
    renderApp("/settings/overview");
    const tile = (await screen.findByText(/Last night · 40 classified/)).closest(".tile")!;
    expect(tile).toHaveClass("fail");
  });

  it("saves a second move from the first's order, after it, so neither undoes the other", async () => {
    viewport(true);
    const topicOf = (slug: string, depth: string) => ({ ...topic, class: slug, depth, monogram: slug.toUpperCase() });
    const classOf = (slug: string) => ({ ...cls, slug, name: slug.toUpperCase() });
    const four = { classes: ["a", "b", "c", "d"].map(classOf), topics: [topicOf("a", "focus"), topicOf("b", "focus"), topicOf("c", "area"), topicOf("d", "area")] };
    // The server's order follows each save as it lands; both saves wait to land until told to.
    let server = ["a", "b", "c", "d"];
    const release: (() => void)[] = [];
    const calls = scriptFetch({
      ...common,
      "GET /api/settings/taxonomy": () => ({ ...four, topics: server.map((slug) => four.topics.find((t) => t.class === slug)!) }),
      "PUT /api/settings/topics-order": (c) => new Promise<Response>((r) => release.push(() => ((server = (c.body as { slugs: string[] }).slugs), r(new Response(null, { status: 204 }))))),
    });
    renderApp("/settings/topics");
    await screen.findByRole("button", { name: "Move A" });
    const rows = () => screen.getAllByRole("button", { name: /^Move/ }).map((b) => b.getAttribute("aria-label"));
    const reads = () => calls.filter((c) => c.method === "GET" && c.url === "/api/settings/taxonomy").length;
    const puts = () => calls.filter((c) => c.method === "PUT").map((c) => c.body);
    // jsdom has no hit-testing or pointer capture: the drag lands on the row named.
    Element.prototype.setPointerCapture = () => {};
    const drag = (from: string, over: string) => {
      document.elementFromPoint = () => document.querySelector(`[data-slug="${over}"]`);
      const grip = screen.getByRole("button", { name: `Move ${from}` });
      fireEvent.pointerDown(grip, { pointerId: 1 });
      fireEvent.pointerMove(grip, { pointerId: 1, clientX: 1, clientY: 1 });
      fireEvent.pointerUp(grip, { pointerId: 1 });
    };
    drag("A", "B");
    await waitFor(() => expect(puts().length).toBe(1));
    drag("C", "D");
    // The second save waits for the first, and starts from its order.
    await waitFor(() => expect(rows()).toEqual(["Move B", "Move A", "Move D", "Move C"]));
    expect(puts().length).toBe(1);
    const before = reads();
    release[0]!();
    await waitFor(() => expect(puts()).toEqual([{ slugs: ["b", "a", "c", "d"] }, { slugs: ["b", "a", "d", "c"] }]));
    // The first save landed reads the tree again only once no other waits: the server's order of this moment would
    // put C back under the reader until the second landed.
    expect(reads()).toBe(before);
    expect(rows()).toEqual(["Move B", "Move A", "Move D", "Move C"]);
    release[1]!();
    await waitFor(() => expect(reads()).toBeGreaterThan(before));
    await waitFor(() => expect(rows()).toEqual(["Move B", "Move A", "Move D", "Move C"]));
  });

  // The four-topic tree the reorder tests drag in, with a server whose order follows each save as it lands and
  // saves that wait to land, or to fail, until told to.
  const reorderable = () => {
    const topicOf = (slug: string, depth: string) => ({ ...topic, class: slug, depth, monogram: slug.toUpperCase() });
    const classOf = (slug: string) => ({ ...cls, slug, name: slug.toUpperCase() });
    const four = { classes: ["a", "b", "c", "d"].map(classOf), topics: [topicOf("a", "focus"), topicOf("b", "focus"), topicOf("c", "area"), topicOf("d", "area")] };
    let server = ["a", "b", "c", "d"];
    const release: ((ok?: boolean) => void)[] = [];
    const calls = scriptFetch({
      ...common,
      "GET /api/settings/config": {},
      "GET /api/settings/taxonomy": () => ({ ...four, topics: server.map((slug) => four.topics.find((t) => t.class === slug)!) }),
      "PUT /api/settings/topics-order": (c) =>
        new Promise<Response>((r) =>
          release.push((ok = true) => {
            if (ok) server = (c.body as { slugs: string[] }).slugs;
            r(ok ? new Response(null, { status: 204 }) : json({ detail: "nope" }, 500));
          }),
        ),
    });
    const rows = () => screen.getAllByRole("button", { name: /^Move/ }).map((b) => b.getAttribute("aria-label"));
    const reads = () => calls.filter((c) => c.method === "GET" && c.url === "/api/settings/taxonomy").length;
    const puts = () => calls.filter((c) => c.method === "PUT").map((c) => c.body);
    // jsdom has no hit-testing or pointer capture: the drag lands on the row named.
    Element.prototype.setPointerCapture = () => {};
    const drag = (from: string, over: string) => {
      document.elementFromPoint = () => document.querySelector(`[data-slug="${over}"]`);
      const grip = screen.getByRole("button", { name: `Move ${from}` });
      fireEvent.pointerDown(grip, { pointerId: 1 });
      fireEvent.pointerMove(grip, { pointerId: 1, clientX: 1, clientY: 1 });
      fireEvent.pointerUp(grip, { pointerId: 1 });
    };
    return { release, rows, reads, puts, drag };
  };

  it("keeps a moved row where it was dropped over a fresh visit's read, until the save has landed", async () => {
    viewport(true);
    const { release, rows, reads, puts, drag } = reorderable();
    renderApp("/settings/topics");
    await screen.findByRole("button", { name: "Move A" });
    drag("A", "B");
    await waitFor(() => expect(puts().length).toBe(1));
    expect(rows()).toEqual(["Move B", "Move A", "Move C", "Move D"]);
    // Away and back while the save is in flight: the fresh visit reads the tree, in the server's order of now.
    const before = reads();
    await userEvent.click(screen.getByRole("link", { name: /Configuration/ }));
    await waitFor(() => expect(document.querySelector("main .h1")).toHaveTextContent("Configuration"));
    await userEvent.click(screen.getByRole("link", { name: /Topics/ }));
    await screen.findByRole("button", { name: "Move A" });
    await waitFor(() => expect(reads()).toBeGreaterThan(before));
    expect(rows()).toEqual(["Move B", "Move A", "Move C", "Move D"]);
    release[0]!();
    await waitFor(() => expect(reads()).toBeGreaterThan(before + 1));
    expect(rows()).toEqual(["Move B", "Move A", "Move C", "Move D"]);
  });

  it("neither rolls back nor reads the tree when a save fails while a later one waits its turn", async () => {
    viewport(true);
    const { release, rows, reads, puts, drag } = reorderable();
    renderApp("/settings/topics");
    await screen.findByRole("button", { name: "Move A" });
    drag("A", "B");
    await waitFor(() => expect(puts().length).toBe(1));
    drag("C", "D");
    await waitFor(() => expect(rows()).toEqual(["Move B", "Move A", "Move D", "Move C"]));
    const before = reads();
    release[0]!(false);
    await screen.findByText("nope");
    await waitFor(() => expect(puts().length).toBe(2));
    // The failure is said; the page's order stands while the second save, which carries both moves, is in flight.
    expect(reads()).toBe(before);
    expect(rows()).toEqual(["Move B", "Move A", "Move D", "Move C"]);
    release[1]!();
    await waitFor(() => expect(reads()).toBeGreaterThan(before));
    await waitFor(() => expect(rows()).toEqual(["Move B", "Move A", "Move D", "Move C"]));
  });

  it("says when the topics' revisions could not be loaded, showing no version meanwhile", async () => {
    viewport(true);
    let fail = true;
    scriptFetch({ ...common, "GET /api/settings/taxonomy/stats": () => (fail ? json({ detail: "gone" }, 500) : stats) });
    renderApp("/settings/topics");
    expect(await screen.findByText(/Could not load the topics' revisions/)).toBeInTheDocument();
    const row = screen.getByText("Mech interp", { selector: ".txt" }).closest(".trow")!;
    expect(row).not.toHaveTextContent(/v\d/);
    fail = false;
    await userEvent.click(screen.getByRole("button", { name: "Try again" }));
    await waitFor(() => expect(row).toHaveTextContent("v2"));
    expect(screen.queryByText(/Could not load/)).toBeNull();
  });

  it("says when the runs could not be loaded, rather than that last night is gone from them", async () => {
    viewport(true);
    scriptFetch({ ...common, "GET /api/settings/runs": json({ detail: "gone" }, 500) });
    renderApp("/settings/overview");
    expect(await screen.findByText(/Could not load the runs/)).toBeInTheDocument();
    expect(screen.queryByText(/no longer among the runs listed/)).toBeNull();
    expect(screen.getByText("No Calibrate session yet")).toBeInTheDocument();
  });

  it("shows a draft topic as one, activated only by an explicit choice", async () => {
    viewport(true);
    const calls = scriptFetch({ ...common, "GET /api/settings/taxonomy": { ...tree, topics: [{ ...topic, status: "draft" }] }, "GET /api/settings/topics/mi/keywords": {}, "PUT /api/settings/topics/mi": { ...topic, status: "active" } });
    renderApp("/settings/topics/mi");
    const status = await screen.findByRole("group", { name: "Status" });
    expect(within(status).getByRole("button", { name: "draft" })).toHaveAttribute("aria-pressed", "true");
    expect(within(status).getByRole("button", { name: "active" })).toHaveAttribute("aria-pressed", "false");
    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
    await userEvent.click(within(status).getByRole("button", { name: "active" }));
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(calls.find((c) => c.method === "PUT")?.body).toMatchObject({ status: "active" }));
  });

  it("shows Deep cards' month to date at list prices, at least while an attempt's usage is unknown", async () => {
    viewport(true);
    const night = { id: 6, kind: "nightly", started_at: "2026-10-14T03:00:00Z", finished_at: "2026-10-14T03:30:00Z", cost: 1.2, error: null, counts: {}, fetched: 0, inserted: 0, updated: 0 };
    scriptFetch({ ...common, "GET /api/settings/quality": { ...quality, deep: { attempts: 4, list_cost: 2.5, unknown: 1 } }, "GET /api/settings/runs": [night] });
    const { unmount } = renderApp("/settings/runs");
    expect(await screen.findByText(/at least \$2\.50 at list prices · 4 attempts/)).toBeInTheDocument();
    expect(screen.getByText(/1 with usage unknown/)).toBeInTheDocument();
    unmount();
    scriptFetch({ ...common, "GET /api/settings/quality": { ...quality, deep: { attempts: 0, list_cost: 0, unknown: 0 } }, "GET /api/settings/runs": [night] });
    renderApp("/settings/runs");
    expect(await screen.findByText(/^\$0\.00 at list prices · 0 attempts/)).toBeInTheDocument();
    expect(screen.queryByText(/at least/)).toBeNull();
  });
});
