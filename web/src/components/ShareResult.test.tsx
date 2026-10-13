import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { json, renderApp, scriptFetch } from "@/test/harness";

afterEach(() => vi.unstubAllGlobals());

const status = { state: "quiet", read: 1 };
const card = (slug: string, name: string, kind: string) => ({ slug, name, kind, colour: "#2f5d50", status: "active", cadence: "weekly", position: 1, subject: "", papers: 0, saved: 0, new: 0, new_saved: 0 });
const topics = [card("cot", "CoT monitoring", "watch"), card("faith", "CoT faithfulness", "watch"), card("interp", "Interpretability", "radar")];
const row = { id: "2610.00001", title: "A strong interpretability paper", authors: ["Ada Lovelace"], published: "2026-10-01T00:00:00Z", added: "2026-10-02T00:00:00Z", grade: "relevant", grading: false, take: null, saved: true, also_in: [], source: "collection" };
const topicState = (slug: string, extra: Record<string, unknown> = {}) => ({ slug, name: topics.find((t) => t.slug === slug)!.name, kind: topics.find((t) => t.slug === slug)!.kind, colour: null, grade: null, take: null, label: null, reaction: null, member: false, by_owner: false, ...extra });

/** The paper as its page gives it, changing as the actions land, as the server would. */
function script() {
  const paper: { topics: Record<string, unknown>[]; [k: string]: unknown } = {
    id: row.id, title: row.title, abstract: "", authors: row.authors, categories: [], added: row.added, arxiv_url: "", pdf_url: "", history: [], chosen: [],
    saved: true,
    topics: [topicState("cot", { grade: "relevant" }), topicState("faith")],
  };
  const calls = scriptFetch({
    "GET /api/status": () => json(status),
    "GET /api/topics": () => json(topics),
    "GET /api/papers": () => json({ rows: [], total: 0, outside: [], left_out: [] }),
    "POST /api/lookup": () => json({ row, fetched: false }),
    "GET /api/papers/2610.00001": () => json(paper),
    "PUT /api/papers/2610.00001/fields/interp": () => {
      paper.topics = [...paper.topics, topicState("interp", { member: true, by_owner: true, reaction: "up" })];
      return json({ message: "Important for Interpretability", saved: true, grades: {} });
    },
    "DELETE /api/papers/2610.00001/fields/interp": () => {
      paper.topics = paper.topics.filter((t) => t.slug !== "interp");
      return json({ message: "No longer marked important for Interpretability", saved: true, grades: {} });
    },
    "POST /api/misses": () => {
      paper.topics = paper.topics.map((t) => (t.slug === "faith" ? { ...t, grade: "relevant", label: { value: "relevant", grade: "relevant", origin: "miss", provisional: false } } : t));
      return json({ message: "Reported as missed for CoT faithfulness", saved: true, grades: {} });
    },
    "PUT /api/library/2610.00001": () => json({ message: "Removed from the library", saved: false, grades: {} }),
  });
  return calls;
}

describe("the share result", () => {
  it("shows the paper as a card and one big action per topic, done states and all", async () => {
    const calls = script();
    const user = userEvent.setup();
    renderApp("/papers?url=https://arxiv.org/abs/2610.00001");
    const section = within(await screen.findByRole("region", { name: "Shared paper" }));
    expect(await section.findByText("Already in the collection")).toBeInTheDocument();
    expect(section.getByRole("link", { name: /A strong interpretability paper/ })).toHaveAttribute("href", "/paper/2610.00001?topic=none");
    // Saved already: the row reads as done with an undo. Relevant for one Watch topic: no miss offered there.
    expect(await section.findByText("Saved")).toBeInTheDocument();
    expect(section.getByText("Already relevant for CoT monitoring.")).toBeInTheDocument();
    expect(section.queryByRole("button", { name: /Missed by CoT monitoring/ })).not.toBeInTheDocument();

    // "Important for <field>" lands, reads as done, and can be taken back.
    await user.click(section.getByRole("button", { name: "Important for Interpretability" }));
    expect(await section.findByText("in the field as your addition; saved")).toBeInTheDocument();
    expect(calls.some((c) => c.method === "PUT" && c.url === "/api/papers/2610.00001/fields/interp")).toBe(true);
    await user.click(section.getAllByRole("button", { name: "Take back" })[0]!);
    await waitFor(() => expect(calls.some((c) => c.method === "DELETE" && c.url === "/api/papers/2610.00001/fields/interp")).toBe(true));
    expect(await section.findByRole("button", { name: "Important for Interpretability" })).toBeInTheDocument();

    // A miss for the other Watch topic.
    await user.click(section.getByRole("button", { name: "Missed by CoT faithfulness" }));
    expect(await section.findByText("Reported as missed by CoT faithfulness")).toBeInTheDocument();
    expect(calls.find((c) => c.method === "POST" && c.url === "/api/misses")!.body).toEqual({ paper_id: "2610.00001", topic: "faith" });

    // Unsave from the saved row.
    await user.click(section.getByRole("button", { name: "Unsave" }));
    await waitFor(() => expect(calls.find((c) => c.method === "PUT" && c.url === "/api/library/2610.00001")!.body).toEqual({ saved: false }));
  });
});
