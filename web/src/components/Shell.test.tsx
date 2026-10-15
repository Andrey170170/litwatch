import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { renderApp, scriptFetch, viewport } from "@/test/harness";

const badge = { slug: "mi", name: "Mech interp", depth: "focus", monogram: "MI", colour: "#f3e600" };
const status = { state: "quiet", papers: 212, awaiting_classification: 0, analysis_failed: 0, ungraded: 0, fulltext_failed: 0, digests_failed: 0 };
const summary = { status, topics: [badge], settings: { waiting: 0, last_session: null, misses: 0, topics: 1, classes: 1, spent: 0, budget: null, failing: [] } };
const counts = { legendary: 0, epic: 0, rare: 0, uncommon: 0, common: 0, unrated: 0 };
const grades = { must_read: 0, relevant: 0, adjacent: 0, none: 0, ungraded: 0 };
const topic = { ...badge, status: "active", subject: "Circuits." };

describe("The shell's sidebar", () => {
  it("reads the summary once, and neither refetches nor re-renders as the reader switches between pages", async () => {
    viewport(true);
    const renders: Record<string, number> = {};
    (globalThis as { __renders?: Record<string, number> }).__renders = renders;
    const calls = scriptFetch({
      "GET /api/summary": summary,
      "GET /api/status": status,
      "GET /api/topics": [{ depth: "focus", topics: [] }],
      "GET /api/papers": { rows: [], total: 0, counts, grades, outside: [] },
      "GET /api/topics/mi": { topic, rows: [], total: 0, counts, grades },
    });
    renderApp("/papers");
    const following = await screen.findByRole("link", { name: /Mech interp/ });
    const line = screen.getByRole("link", { name: "212" });
    const after = { ...renders };
    // Papers → the topic → Papers → the topic, quickly.
    for (let i = 0; i < 2; i++) {
      await userEvent.click(following);
      await waitFor(() => expect(document.querySelector("main .h1")).toHaveTextContent("Mech interp"));
      await userEvent.click(screen.getByRole("link", { name: "Papers" }));
      await waitFor(() => expect(document.querySelector("main .h1")).toHaveTextContent("Papers"));
    }
    expect(calls.filter((c) => c.url === "/api/summary")).toHaveLength(1);
    expect(screen.getByRole("link", { name: /Mech interp/ })).toBe(following);
    expect(screen.getByRole("link", { name: "212" })).toBe(line);
    expect(renders).toEqual(after);
  });

  it("updates a changed count in place when the summary comes back different", async () => {
    viewport(true);
    let waiting = 0;
    scriptFetch({
      "GET /api/summary": () => ({ ...summary, status: { ...status, awaiting_classification: waiting }, settings: { ...summary.settings, waiting } }),
      "GET /api/status": status,
      "GET /api/topics": [{ depth: "focus", topics: [] }],
      "GET /api/settings/config": {},
    });
    const client = (await import("@/test/harness")).freshClient();
    renderApp("/settings/configuration", client);
    const needs = await screen.findByRole("link", { name: /Needs you/ });
    expect(within(needs).queryByText(/\d/)).toBeNull();
    waiting = 2;
    // What a write does once recorded: the summary is refetched; the count appears beside the page's name.
    await client.invalidateQueries({ queryKey: ["summary"] });
    await waitFor(() => expect(within(needs).getByText("2")).toBeInTheDocument());
    expect(screen.getByRole("link", { name: /Needs you/ })).toBe(needs);
    expect(screen.getByRole("link", { name: "2 failed" })).toBeInTheDocument();
  });
});
