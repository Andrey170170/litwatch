import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { json, renderApp, scriptFetch } from "@/test/harness";

afterEach(() => vi.unstubAllGlobals());

const status = { state: "quiet", read: 1, splits: 0, ungraded: 0, awaiting_triage: 0, reviews_failed: 0 };
const record = { dblp_key: "conf/iclr/Demo26", title: "A Paper", kind: "inproceedings", venue: "ICLR", container: "ICLR 2026", year: 2026, doi: "10.1/demo", url: null };
const paper: Record<string, unknown> = {
  id: "2610.00001",
  title: "A Paper",
  abstract: "The abstract.",
  authors: ["Ada Lovelace"],
  categories: ["cs.CL"],
  published: "2026-10-01T00:00:00Z",
  added: "2026-10-02T00:00:00Z",
  arxiv_url: "https://arxiv.org/abs/2610.00001",
  pdf_url: "https://arxiv.org/pdf/2610.00001",
  topics: [],
  history: [],
  chosen: [],
  publication: null,
  publication_by_owner: false,
  card: null,
  card_requestable: false,
};

function script(overrides: Record<string, unknown>, routes: Record<string, () => unknown> = {}) {
  let current = { ...paper, ...overrides };
  const calls = scriptFetch({
    "GET /api/status": () => json(status),
    "GET /api/topics": () => json([]),
    "GET /api/papers/2610.00001": () => json(current),
    "PUT /api/papers/2610.00001/publication": () => {
      current = { ...current, publication: record, publication_by_owner: true };
      return json(record);
    },
    ...routes,
  });
  return calls;
}

describe("the paper page", () => {
  it("links the published version from the action bar and shows the record", async () => {
    script({ publication: record });
    renderApp("/paper/2610.00001");
    const link = await screen.findByRole("link", { name: "ICLR 2026" });
    expect(link).toHaveAttribute("href", "https://doi.org/10.1/demo");
    // The container already names the year, so it is not repeated.
    expect(screen.getByText(/^ICLR 2026 ·$/)).toBeInTheDocument();
    expect(screen.queryByText("set by you")).not.toBeInTheDocument();
  });

  it("takes a DOI from the owner and marks the version as set by them", async () => {
    const calls = script({});
    const user = userEvent.setup();
    renderApp("/paper/2610.00001");
    expect(await screen.findByText("Not yet found.")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Know it?" }));
    await user.type(screen.getByLabelText("DOI or DBLP key"), "https://doi.org/10.1/demo");
    await user.click(screen.getByRole("button", { name: "Use it" }));
    await waitFor(() => expect(calls.find((c) => c.method === "PUT")).toBeTruthy());
    expect(calls.find((c) => c.method === "PUT")!.body).toEqual({ dblp_key: null, doi: "10.1/demo", none: false });
    expect(await screen.findByText("set by you")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Back to automatic" })).toBeInTheDocument();
  });

  it("says in plain words when DBLP has no such key", async () => {
    script({}, { "PUT /api/papers/2610.00001/publication": () => json({ detail: "DBLP has no such record" }, 404) });
    const user = userEvent.setup();
    renderApp("/paper/2610.00001");
    await user.click(await screen.findByRole("button", { name: "Know it?" }));
    await user.type(screen.getByLabelText("DOI or DBLP key"), "conf/nope/X");
    await user.click(screen.getByRole("button", { name: "Use it" }));
    // Inline by the control, and as a toast.
    expect((await screen.findAllByText("DBLP has no record with that key.")).length).toBeGreaterThan(0);
  });

  it("renders a stored deep card as prose", async () => {
    script({
      card: {
        card: { summary: "First paragraph.\n\nSecond paragraph.", key_claim: "The claim.", limitations: ["One.", "Two."], code_available: true },
        model: "openai/gpt-6.1-sol",
        written_at: "2026-10-09T08:00:00Z",
      },
    });
    renderApp("/paper/2610.00001");
    expect(await screen.findByText("First paragraph.")).toBeInTheDocument();
    expect(screen.getByText("Second paragraph.")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Key claim" })).toBeInTheDocument();
    expect(screen.getByText("The claim.")).toBeInTheDocument();
    expect(screen.getByText("One., Two.")).toBeInTheDocument();
    expect(screen.getByText(/Code available: yes/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Request a deep card" })).not.toBeInTheDocument();
  });

  it("offers to request a card only while one can be made, and waits after the 202", async () => {
    const calls = script({ card_requestable: true }, { "POST /api/papers/2610.00001/card": () => json({ event_id: 9 }, 202) });
    const user = userEvent.setup();
    renderApp("/paper/2610.00001");
    await user.click(await screen.findByRole("button", { name: "Request a deep card" }));
    expect(await screen.findByText(/The card appears here once it is written/)).toBeInTheDocument();
    expect(calls.some((c) => c.method === "POST" && c.url === "/api/papers/2610.00001/card")).toBe(true);
  });
});
