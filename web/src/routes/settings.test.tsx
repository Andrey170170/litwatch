import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { renderApp, scriptFetch, viewport } from "@/test/harness";

const status = { state: "quiet", papers: 2, awaiting_classification: 0, analysis_failed: 0, ungraded: 0 };
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
const quality = {
  night: { run: 6, error: null, splits: [split], awaiting_classification: [], analysis_failed: [stuck], ungraded: [] },
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
    await waitFor(() => expect(needs).toHaveTextContent("2 waiting"));
    expect(screen.getByRole("link", { name: /Topics/ })).toHaveTextContent("1 followed");
    expect(screen.getByRole("link", { name: /Runs and cost/ })).toHaveTextContent("$1.50 of $20.00 this month");
  });

  it("settles a split in place: the label is recorded, the item keeps its spot and the count drops", async () => {
    viewport(true);
    const calls = scriptFetch({ ...common, "PUT /api/labels/mi/2610.00001": { saved: false } });
    renderApp("/settings/needs");
    expect(await screen.findByText("2 things wait on the owner. Each also shows on its paper's page.")).toBeInTheDocument();
    const item = screen.getByText("A split paper").closest(".need")!;
    expect(item).toHaveTextContent("adjacent stands");
    await userEvent.click(within(item as HTMLElement).getByRole("button", { name: "related" }));
    await waitFor(() => expect(calls.find((c) => c.method === "PUT")?.body).toEqual({ value: "related" }));
    await waitFor(() => expect(item).toHaveClass("settled"));
    expect(item).toHaveTextContent("labelled related");
    // Still first in its group, before the stuck paper.
    const titles = screen.getAllByRole("link", { name: /paper$/ }).map((l) => l.textContent);
    expect(titles).toEqual(["A split paper", "A stuck paper"]);
    expect(screen.getByText("1 thing waits on the owner. Each also shows on its paper's page.")).toBeInTheDocument();
  });

  it("saves a topic only once something changed, then offers the re-run in place of Save", async () => {
    viewport(true);
    const calls = scriptFetch({ ...common, "GET /api/settings/topics/mi/keywords": { circuit: 4 }, "PUT /api/settings/topics/mi": () => ({ ...topic, interests: "Sparse circuits" }) });
    renderApp("/settings/topics/mi");
    const save = await screen.findByRole("button", { name: "Save" });
    expect(save).toBeDisabled();
    await userEvent.type(screen.getByRole("textbox", { name: "Interests" }), "Sparse circuits");
    expect(save).toBeEnabled();
    await userEvent.click(save);
    await waitFor(() => expect(calls.find((c) => c.method === "PUT")?.body).toMatchObject({ class: "mi", interests: "Sparse circuits", keywords: ["circuit"] }));
    expect(await screen.findByText("Saved as version 2. It applies to papers classified or graded from now on.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Save" })).toBeNull();
  });

  it("records a Calibrate answer on Next, then shows the next paper", async () => {
    viewport(true);
    const items = [1, 2].map((position) => ({ position, paper_id: `2610.0000${position}`, topic: "mi", why: "above", answered_at: null, title: `Drawn ${position}`, abstract: "A.", authors: ["A"], published: "2026-10-13", fit: {}, appraisal: null }));
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
