import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { json, renderApp, scriptFetch } from "@/test/harness";

afterEach(() => vi.unstubAllGlobals());

const status = { state: "quiet", read: 1 };
const row = (id: string, title: string, source = "collection") => ({ id, title, authors: ["Ada Lovelace"], published: "2026-10-01T00:00:00Z", added: source === "collection" ? "2026-10-02T00:00:00Z" : null, grade: null, grading: false, take: null, saved: false, also_in: [], source });

function stubDownloads() {
  const urls: unknown[] = [];
  vi.stubGlobal("URL", Object.assign(URL, { createObjectURL: (b: unknown) => (urls.push(b), "blob:x"), revokeObjectURL: () => {} }));
  return urls;
}

describe("a search with providers", () => {
  it("exports BibTeX over the outside band too, by selection or with the filter", async () => {
    stubDownloads();
    const calls = scriptFetch({
      "GET /api/status": () => json(status),
      "GET /api/topics": () => json([]),
      "GET /api/papers": () => json({ rows: [row("2610.00001", "In the collection")], total: 1, outside: [row("2610.00002", "Found by the archive", "archive")], left_out: [] }),
      "GET /api/bibtex": () => new Response("@misc{collection}", { headers: { "content-type": "application/x-bibtex" } }),
      "POST /api/bibtex": () => new Response("@misc{outside}", { headers: { "content-type": "application/x-bibtex" } }),
    });
    const user = userEvent.setup();
    renderApp("/papers?q=traces");
    await screen.findByText(/Outside the collection · 1/);
    // With nothing selected: the filter's export, then the band shown, cited as it is.
    await user.click(screen.getByRole("button", { name: "Export BibTeX" }));
    await waitFor(() => expect(calls.find((c) => c.method === "POST" && c.url === "/api/bibtex")).toBeTruthy());
    expect(calls.some((c) => c.method === "GET" && c.url.startsWith("/api/bibtex"))).toBe(true);
    expect(calls.find((c) => c.method === "POST" && c.url === "/api/bibtex")!.body).toEqual({
      ids: [],
      cited: [{ id: "2610.00002", title: "Found by the archive", authors: ["Ada Lovelace"], year: 2026 }],
    });
    // An outside row can be selected and goes out as a citation beside the collection's ids.
    await user.click(screen.getByRole("checkbox", { name: "Select Found by the archive" }));
    await user.click(screen.getByRole("checkbox", { name: "Select In the collection" }));
    await user.click(screen.getByRole("button", { name: "BibTeX for 2 selected" }));
    await waitFor(() => expect(calls.filter((c) => c.method === "POST" && c.url === "/api/bibtex")).toHaveLength(2));
    expect(calls.filter((c) => c.method === "POST" && c.url === "/api/bibtex")[1]!.body).toEqual({
      ids: ["2610.00001"],
      cited: [{ id: "2610.00002", title: "Found by the archive", authors: ["Ada Lovelace"], year: 2026 }],
    });
  });

  it("exports a search that found only outside papers, dated by their UTC year", async () => {
    stubDownloads();
    // Published early on 1 January in UTC: still the previous year west of Greenwich.
    const early = { ...row("2610.00003", "New year's paper", "archive"), published: "2026-01-01T03:00:00Z" };
    const calls = scriptFetch({
      "GET /api/status": () => json(status),
      "GET /api/topics": () => json([]),
      "GET /api/papers": () => json({ rows: [], total: 0, outside: [early], left_out: [] }),
      "GET /api/bibtex": () => new Response("", { headers: { "content-type": "application/x-bibtex" } }),
      "POST /api/bibtex": () => new Response("@misc{outside}", { headers: { "content-type": "application/x-bibtex" } }),
    });
    const user = userEvent.setup();
    renderApp("/papers?q=traces");
    await screen.findByText(/Outside the collection · 1/);
    const exportButton = screen.getByRole("button", { name: "Export BibTeX" });
    expect(exportButton).toBeEnabled();
    await user.click(exportButton);
    await waitFor(() => expect(calls.find((c) => c.method === "POST" && c.url === "/api/bibtex")).toBeTruthy());
    expect(calls.find((c) => c.method === "POST" && c.url === "/api/bibtex")!.body).toMatchObject({
      cited: [{ id: "2610.00003", year: 2026 }],
    });
  });

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
