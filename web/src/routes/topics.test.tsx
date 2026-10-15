import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { json, renderApp, scriptFetch, viewport } from "@/test/harness";

const card = { slug: "mi", name: "Mech interp", depth: "focus", monogram: "MI", colour: "#f3e600", papers: 3, saved: 1, counts: { legendary: 0, epic: 1, rare: 2, uncommon: 0, common: 0, unrated: 0 }, newest_must_read: null };

describe("Topics", () => {
  it("says when the topics could not be loaded, and tries again on request", async () => {
    viewport(true);
    let fail = true;
    scriptFetch({
      "GET /api/status": { state: "none", papers: 0, awaiting_classification: 0, analysis_failed: 0, ungraded: 0 },
      "GET /api/topics": () => (fail ? json({ detail: "gone" }, 500) : [{ depth: "focus", topics: [card] }]),
    });
    renderApp("/topics");
    expect(await screen.findByText(/Could not load the topics/)).toBeInTheDocument();
    expect(screen.queryByText("No topics are followed yet.")).toBeNull();
    fail = false;
    await userEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByRole("link", { name: "Mech interp" })).toBeInTheDocument();
  });
});
