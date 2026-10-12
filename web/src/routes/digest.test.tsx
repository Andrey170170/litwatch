import { screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { json, renderApp, scriptFetch } from "@/test/harness";

afterEach(() => vi.unstubAllGlobals());

const status = { state: "quiet", read: 12, splits: 0, ungraded: 0, awaiting_triage: 0, reviews_failed: 0 };
const watch = { slug: "cot", name: "CoT monitoring", kind: "watch", colour: "#2f5d50" };
const radar = { slug: "interp", name: "Interpretability", kind: "radar", colour: "#3b4f8a" };
const pulse = { slug: "agents", name: "Agents", kind: "pulse", colour: "#a2661e" };

/** A weekly issue: a written recap, a written review with a pick, and a failed review. */
const weekly = {
  id: 7,
  kind: "weekly",
  day: "2026-10-05",
  lede: "A quiet week, apart from one circuit paper.",
  sections: [
    {
      topic: watch,
      entries: [],
      recap: {
        state: "written",
        text: "Two papers were announced this week.\n\nOne of them you set aside.",
        papers: [{ paper_id: "2610.00001", title: "Announced paper" }],
        set_aside: [{ paper_id: "2610.00002", title: "Set-aside paper" }],
        misses: [],
      },
    },
    {
      topic: radar,
      entries: [],
      review: {
        period: "week",
        day: "2026-10-05",
        state: "written",
        text: "A busy week for circuit methods.",
        members: 28,
        picks: [{ paper_id: "2610.00003", title: "Circuit paper", authors: ["Ada Lovelace"], published: "2026-10-01T00:00:00Z", why: "It closes a gap.", saved: false }],
      },
    },
    { topic: pulse, entries: [], review: { period: "week", day: "2026-10-05", state: "failed", text: null, members: 41, picks: [] } },
  ],
};

describe("the weekly issue", () => {
  it("renders recaps and reviews as prose, picks under the review, and a failed one as a line", async () => {
    scriptFetch({
      "GET /api/status": () => json(status),
      "GET /api/digest": () => json({ weekly: { id: 7, kind: "weekly", day: "2026-10-05", lede: weekly.lede }, issues: [], status }),
      "GET /api/issues/7": () => json(weekly),
    });
    renderApp("/?issue=7");
    expect(await screen.findByText("Two papers were announced this week.")).toBeInTheDocument();
    expect(screen.getByText("One of them you set aside.")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Set-aside paper" })).toBeInTheDocument();
    expect(screen.getByText("A busy week for circuit methods.")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Circuit paper" })).toBeInTheDocument();
    expect(screen.getByText("It closes a gap.")).toBeInTheDocument();
    expect(screen.getByText(/The week's review of 41 papers did not get written/)).toBeInTheDocument();
    expect(screen.getByText(/That is the week of 5 October/)).toBeInTheDocument();
  });

  it("pins the latest weekly issue above the feed", async () => {
    scriptFetch({
      "GET /api/status": () => json(status),
      "GET /api/digest": () => json({ weekly: { id: 7, kind: "weekly", day: "2026-10-05", lede: null }, issues: [{ id: 8, kind: "daily", day: "2026-10-07", lede: null }], status }),
      "GET /api/issues/8": () => json({ id: 8, kind: "daily", day: "2026-10-07", lede: null, sections: [] }),
    });
    renderApp("/");
    const pinned = await screen.findByRole("link", { name: /This week/ });
    expect(pinned).toHaveTextContent("Week of 5 October");
    expect(pinned).toHaveAttribute("href", "/?issue=7");
  });
});
