import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { json, renderApp, scriptFetch } from "@/test/harness";

afterEach(() => vi.unstubAllGlobals());

const status = { state: "quiet", read: 1 };
const row = (id: string, title: string, source = "collection") => ({ id, title, authors: ["Ada Lovelace"], published: "2026-10-01T00:00:00Z", added: source === "collection" ? "2026-10-02T00:00:00Z" : null, grade: null, grading: false, take: null, saved: false, also_in: [], source });

describe("a search with providers", () => {
  it("bands the papers found outside the collection, fetches one, and notes who was left out", async () => {
    const calls = scriptFetch({
      "GET /api/status": () => json(status),
      "GET /api/topics": () => json([]),
      "GET /api/papers": () => json({ rows: [row("2610.00001", "In the collection")], total: 1, outside: [row("2610.00002", "Found by the archive", "archive")], left_out: ["slowprovider"] }),
      "POST /api/lookup": () => json({ row: row("2610.00002", "Found by the archive"), fetched: true }),
    });
    const user = userEvent.setup();
    renderApp("/papers?q=traces");
    expect(await screen.findByText(/Outside the collection · 1/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Found by the archive" })).toHaveAttribute("href", "https://arxiv.org/abs/2610.00002");
    expect(screen.getByText("found by archive")).toBeInTheDocument();
    expect(screen.getByText(/slowprovider did not answer this search in time/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Fetch" }));
    await waitFor(() => expect(calls.find((c) => c.method === "POST" && c.url === "/api/lookup")).toBeTruthy());
    expect(calls.find((c) => c.method === "POST")!.body).toEqual({ q: "2610.00002" });
  });
});
