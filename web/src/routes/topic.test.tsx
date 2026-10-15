import { screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { renderApp, scriptFetch, viewport } from "@/test/harness";

const topic = { slug: "mi", name: "Mech interp", depth: "focus", monogram: "MI", colour: "#f3e600", status: "active", subject: "Circuits." };
const counts = { legendary: 2, epic: 5, rare: 14, uncommon: 61, common: 188, unrated: 0 };
const grades = { must_read: 7, relevant: 14, adjacent: 61, none: 100, ungraded: 88 };

afterEach(() => sessionStorage.clear());

describe("A topic page", () => {
  it("counts the papers under the rarity filter, not the whole topic", async () => {
    viewport(true);
    scriptFetch({
      "GET /api/status": { state: "none", papers: 0, awaiting_classification: 0, analysis_failed: 0, ungraded: 0 },
      "GET /api/topics": [],
      "GET /api/topics/mi": { topic, rows: [], total: 0, counts, grades },
    });
    renderApp("/topics/mi?rarity=rare");
    expect(await screen.findByText("14 papers")).toBeInTheDocument();
    expect(screen.queryByText("270 papers")).toBeNull();
  });
});
