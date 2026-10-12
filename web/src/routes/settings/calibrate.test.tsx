import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { json, renderApp, scriptFetch, type Call } from "@/test/harness";

afterEach(() => vi.unstubAllGlobals());

const topics = [
  { slug: "a", name: "Topic A", kind: "watch", colour: "#2f5d50", status: "active", position: 1, subject: "", papers: 1, saved: 0, new: 0, new_saved: 0 },
  { slug: "b", name: "Topic B", kind: "watch", colour: "#8a3b2e", status: "active", position: 2, subject: "", papers: 1, saved: 0, new: 0, new_saved: 0 },
];

function item(position: number, answered = false) {
  return {
    position,
    paper_id: `p${position}`,
    topic: "a",
    why: "above",
    answered_at: answered ? "2026-10-12T00:00:00Z" : null,
    title: `Paper ${position}`,
    abstract: "An abstract.",
    authors: ["Ada Lovelace"],
    published: "2026-10-01T00:00:00Z",
    takes: [],
    fit: answered ? { a: { value: "relevant", grade: "relevant" } } : {},
    appraisal: null,
  };
}

/** A two-paper session whose items become answered as the PUTs land, as the server would. */
function script() {
  const answered = new Set<number>();
  const calls = scriptFetch({
    "GET /api/status": () => json({ state: "none", read: 0, splits: 0, ungraded: 0, awaiting_triage: 0 }),
    "GET /api/topics": () => json(topics),
    "GET /api/settings/calibrate/1": () => json({ id: 1, seed: 1, drawn_at: "2026-10-12T00:00:00Z", items: [item(1, answered.has(1)), item(2, answered.has(2))] }),
    "PUT /api/settings/calibrate/1/items/1": () => {
      answered.add(1);
      return new Response(null, { status: 204 });
    },
    "PUT /api/settings/calibrate/1/items/2": () => {
      answered.add(2);
      return new Response(null, { status: 204 });
    },
  });
  return calls;
}

const puts = (calls: Call[]) => calls.filter((c) => c.method === "PUT");
const fitA = () => within(screen.getByRole("group", { name: "Fit for Topic A" }));

describe("Calibrate", () => {
  it("stays on the paper after its required fit is saved", async () => {
    const calls = script();
    renderApp("/settings/calibrate/1");
    await screen.findByText("1 of 2");
    const user = userEvent.setup();
    await user.click(fitA().getByRole("button", { name: "About it" }));
    await waitFor(() => expect(puts(calls)).toHaveLength(1));
    // The refetch now says paper 1 is answered; the screen must not jump to paper 2.
    await screen.findByLabelText("saved");
    expect(screen.getByText("1 of 2")).toBeInTheDocument();
    expect(screen.getByText("Paper 1")).toBeInTheDocument();
    await user.click(fitA().getByRole("button", { name: "must read" }));
    await waitFor(() => expect(puts(calls)).toHaveLength(2));
    expect((puts(calls)[1]!.body as { fit: { a: { grade: string } } }).fit.a.grade).toBe("must_read");
    await user.click(screen.getByRole("button", { name: /^Next/ }));
    await screen.findByText("2 of 2");
  });

  it("a note typed while changing one's mind never restores the older fit", async () => {
    const calls = script();
    renderApp("/settings/calibrate/1");
    await screen.findByText("1 of 2");
    const user = userEvent.setup();
    await user.click(fitA().getByRole("button", { name: "About it" }));
    await user.type(screen.getByPlaceholderText(/Why it matters/), "hm");
    await user.click(fitA().getByRole("button", { name: "Not" }));
    // Let the note's timer run out, then look at everything that was sent.
    await new Promise((r) => setTimeout(r, 1000));
    await waitFor(() => expect(puts(calls).length).toBeGreaterThanOrEqual(2));
    const last = puts(calls).at(-1)!.body as { fit: { a: { value: string } }; note: string | null };
    expect(last.fit.a.value).toBe("not_relevant");
    expect(last.note).toBe("hm");
    // Once "Not" was chosen, nothing sent afterwards may say otherwise (the blur before the
    // click may legitimately send the earlier fit with the note).
    const values = puts(calls).map((c) => (c.body as typeof last).fit.a.value);
    expect(values.slice(values.indexOf("not_relevant"))).not.toContain("relevant");
  });

  it("flushes a pending note when moving to the next paper", async () => {
    const calls = script();
    renderApp("/settings/calibrate/1");
    await screen.findByText("1 of 2");
    const user = userEvent.setup();
    await user.click(fitA().getByRole("button", { name: "Near it" }));
    await user.type(screen.getByPlaceholderText(/Why it matters/), "later");
    await user.click(screen.getByRole("button", { name: /^Next/ }));
    await screen.findByText("2 of 2");
    await waitFor(() => expect((puts(calls).at(-1)!.body as { note: string | null }).note).toBe("later"));
    expect(puts(calls).at(-1)!.url).toContain("/items/1");
  });
});
