import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { json, renderApp, scriptFetch, viewport } from "@/test/harness";

const topic = { slug: "mi", name: "Mech interp", depth: "focus", monogram: "MI", colour: "#f3e600", status: "active", subject: "Circuits." };
const counts = { legendary: 2, epic: 5, rare: 14, uncommon: 61, common: 188, unrated: 0 };
const grades = { must_read: 7, relevant: 14, adjacent: 61, none: 100, ungraded: 88 };
const row = (id: string, title: string) => ({
  id,
  title,
  authors: ["A. Author"],
  published: "2026-10-13",
  gist: null,
  rarity: "epic",
  badges: [{ ...topic, grade: "must_read", rarity: "epic" }],
  saved: false,
  facets: null,
});
const status = { state: "none", papers: 0, awaiting_classification: 0, analysis_failed: 0, ungraded: 0 };

afterEach(() => sessionStorage.clear());

describe("A topic page", () => {
  it("counts the papers under the rarity filter, not the whole topic", async () => {
    viewport(true);
    scriptFetch({
      "GET /api/status": status,
      "GET /api/topics": [],
      "GET /api/topics/mi": { topic, rows: [], total: 0, counts, grades },
    });
    renderApp("/topics/mi?rarity=rare");
    expect(await screen.findByText("14 papers")).toBeInTheDocument();
    expect(screen.queryByText("270 papers")).toBeNull();
  });

  it("keeps the page and its rows when a later page of the first shelf fails", async () => {
    viewport(true);
    vi.stubGlobal(
      "IntersectionObserver",
      class {
        constructor(private cb: (entries: { isIntersecting: boolean }[]) => void) {}
        observe() {
          this.cb([{ isIntersecting: true }]);
        }
        disconnect() {}
      },
    );
    const many = Array.from({ length: 50 }, (_, i) => row(`2610.${String(i).padStart(5, "0")}`, `Must ${i}`));
    scriptFetch({
      "GET /api/status": status,
      "GET /api/topics": [],
      "GET /api/topics/mi": ({ url }) => {
        const q = new URL(url, "http://x").searchParams;
        if (q.get("grade") !== "must_read") return { topic, rows: [], total: 0, counts, grades };
        if (q.get("offset") === "0") return { topic, rows: many, total: 51, counts, grades };
        return json({ detail: "gone" }, 500);
      },
    });
    renderApp("/topics/mi");
    const top = await screen.findByRole("region", { name: "Must read" });
    expect(await within(top).findByText(/Could not load more/)).toBeInTheDocument();
    expect(screen.getByText("Circuits.")).toBeInTheDocument();
    expect(within(top).getAllByRole("link")).toHaveLength(50);
    expect(screen.queryByText("No such topic.")).toBeNull();
  });

  it("says when there is no such topic", async () => {
    viewport(true);
    scriptFetch({
      "GET /api/status": status,
      "GET /api/topics": [],
      "GET /api/topics/nope": json({ detail: "not found" }, 404),
    });
    renderApp("/topics/nope");
    expect(await screen.findByText("No such topic.")).toBeInTheDocument();
  });
});
