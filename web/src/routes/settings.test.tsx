import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { json, renderApp, scriptFetch, viewport } from "@/test/harness";

const status = { state: "quiet", papers: 2, awaiting_classification: 0, analysis_failed: 0, ungraded: 0, fulltext_failed: 0, digests_failed: 0 };
const cls = { slug: "mi", name: "Mech interp", description: "Circuits and features.", parent: null, threshold: null };
const topic = { class: "mi", depth: "focus", monogram: "MI", colour: "#f3e600", status: "active", subject: "Circuits and features.", rubric: "central: it studies circuits", interests: "", keywords: ["circuit"] };
const tree = { classes: [cls], topics: [topic] };
const stats = { members: { mi: 3 }, thresholds: { mi: 0.5 }, topic_revisions: { mi: 2 } };
const split = {
  paper_id: "2610.00001",
  topic: "mi",
  depth: "focus",
  fit: "adjacent",
  reason: "r",
  split: true,
  escalated: false,
  opinions: { judge: { fit: "adjacent", reason: "a" }, check: { fit: "none", reason: "b" } },
  recorded_at: "2026-10-14T03:00:00Z",
  title: "A split paper",
  label: null,
};
const stuck = { paper_id: "2610.00002", revision: 1, title: "A stuck paper", failed_runs: 3, last_error: "invalid", failed_at: "2026-10-14T03:00:00Z" };
const pass = { id: 7, paper_id: "2610.00003", revision: 1, title: "A textless paper", kind: "brief", state: "failed", failed_runs: 3, last_error: "the LaTeX did not build", queued_at: "2026-10-08T03:00:00Z" };
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
const common = {
  "GET /api/status": status,
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

  it("settles a split in place: the label is recorded, the item keeps its spot and the count drops", async () => {
    viewport(true);
    const calls = scriptFetch({ ...common, "PUT /api/labels/mi/2610.00001": { saved: false }, "DELETE /api/labels/mi/2610.00001": { saved: false } });
    renderApp("/settings/needs");
    expect(await screen.findByText("4 things wait on the owner. Each also shows on its paper's page.")).toBeInTheDocument();
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
    expect(screen.getByRole("link", { name: /Needs you/ })).toHaveTextContent("3");
    // Tapping the label's segment again takes it back: the split waits once more, where it was.
    await userEvent.click(within(item as HTMLElement).getByRole("button", { name: "related" }));
    await waitFor(() => expect(calls.some((c) => c.method === "DELETE" && c.url === "/api/labels/mi/2610.00001")).toBe(true));
    await waitFor(() => expect(item).not.toHaveClass("settled"));
    expect(item).toHaveTextContent("adjacent stands");
    expect(screen.getByText("4 things wait on the owner. Each also shows on its paper's page.")).toBeInTheDocument();
  });

  it("keeps the visit's rows on the way back from a paper, and starts afresh on a link", async () => {
    viewport(true);
    const paper = { id: "2610.00002", title: "A stuck paper", authors: ["A"], abstract: "A.", published: "2026-10-13", version: 1, revision: 1, arxiv_url: "", pdf_url: "", source_url: "", in_collection: true, saved: false, topics: [], states: [], publication_by_owner: false, card_requestable: false };
    // The second read of /quality finds the failure gone and the split too, and lands only when told to.
    let reads = 0;
    let land!: () => void;
    const later = { ...quality, night: { ...quality.night, splits: [], analysis_failed: [] } };
    const calls = scriptFetch({
      ...common,
      "GET /api/settings/quality": () => (++reads > 1 ? new Promise<typeof later>((r) => (land = () => r(later))) : quality),
      "GET /api/papers/2610.00002": paper,
      "POST /api/settings/analysis/2610.00002/retry": { saved: false },
    });
    const { router } = renderApp("/settings/needs");
    const item = (await screen.findByRole("link", { name: "A stuck paper" })).closest(".need")!;
    await userEvent.click(within(item as HTMLElement).getByRole("button", { name: "Try again" }));
    await waitFor(() => expect(item).toHaveClass("settled"));
    const fetched = () => calls.filter((c) => c.url === "/api/settings/quality").length;
    expect(fetched()).toBe(1);
    // To the paper and back: the settled row is still there, settled, with no fetch in between.
    await userEvent.click(screen.getByRole("link", { name: "A stuck paper" }));
    await screen.findByRole("heading", { name: "A stuck paper" });
    router.history.back();
    expect((await screen.findByRole("link", { name: "A stuck paper" })).closest(".need")).toHaveClass("settled");
    expect(fetched()).toBe(1);
    // A Settings page reached by a link is a fresh visit: the reads load again, the sidebar's count with them, on a
    // page that does not read /quality itself.
    const needs = screen.getByRole("link", { name: /Needs you/ });
    expect(within(needs).getByText("3")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("link", { name: /Taxonomy/ }));
    await waitFor(() => expect(fetched()).toBe(2));
    // Back before the read lands: the page waits for it rather than showing the old rows and swapping them.
    router.history.back();
    expect(await screen.findByText("Loading…")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "A stuck paper" })).toBeNull();
    land();
    expect(await screen.findByText("2 things wait on the owner. Each also shows on its paper's page.")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "A stuck paper" })).toBeNull();
    expect(within(needs).getByText("2")).toBeInTheDocument();
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

  it("settles a digest that failed again with the new error, and one written meanwhile with the 409's reason", async () => {
    viewport(true);
    const failed = { id: 3, topic: { slug: "mi", name: "Mech interp", depth: "focus", monogram: "MI", colour: "#f3e600" }, period: "week", day: "2026-10-05", state: "failed", counts: {}, error: "the writer timed out again", written_at: "2026-10-15T10:00:00Z", papers: [] };
    const twice = { ...quality, night: { ...quality.night, digests: [{ ...digest, period: "week", day: "2026-10-05" }, { ...digest, id: 4 }] } };
    scriptFetch({ ...common, "GET /api/settings/quality": twice, "POST /api/digests/3/write": failed, "POST /api/digests/4/write": () => json({ detail: "the digest is not one to write again" }, 409) });
    renderApp("/settings/needs");
    const weekly = (await screen.findByText("Weekly digest · the week of 5 Oct")).closest(".need")!;
    await userEvent.click(within(weekly as HTMLElement).getByRole("button", { name: "Write it again" }));
    await waitFor(() => expect(weekly).toHaveTextContent("Failed again: the writer timed out again. The next run writes it again."));
    const daily = screen.getByText("Daily digest · 8 Oct").closest(".need")!;
    await userEvent.click(within(daily as HTMLElement).getByRole("button", { name: "Write it again" }));
    await waitFor(() => expect(daily).toHaveTextContent("No longer waits on the owner: the digest is not one to write again."));
    expect(screen.getByText("3 things wait on the owner. Each also shows on its paper's page.")).toBeInTheDocument();
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
    expect(screen.getByRole("link", { name: /Needs you/ })).toHaveTextContent("3");
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
    const calls = scriptFetch({ ...common, "GET /api/settings/calibrate/1": { id: 1, seed: 7, drawn_at: "2026-10-15T10:00:00Z", items }, "PUT /api/settings/calibrate/1/items/1": { recorded: true } });
    const { router } = renderApp("/settings/calibrate/1");
    expect(await screen.findByText("Drawn 1")).toBeInTheDocument();
    await userEvent.click(within(screen.getByRole("group", { name: "Fit for Mech interp" })).getByRole("button", { name: "related" }));
    await userEvent.click(screen.getByRole("button", { name: "matters" }));
    await userEvent.click(screen.getByRole("button", { name: "Next ›" }));
    await waitFor(() => expect(calls.find((c) => c.method === "PUT")?.body).toEqual({ fit: { mi: "related" }, value: "matters", note: null }));
    expect(await screen.findByText("Drawn 2")).toBeInTheDocument();
    expect(router.state.location.href).toBe("/settings/calibrate/1?at=2");
  });
});
