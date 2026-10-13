import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { json, renderApp, scriptFetch } from "@/test/harness";

afterEach(() => vi.unstubAllGlobals());

const card = (slug: string, name: string, kind: string) => ({ slug, name, kind, colour: null, status: "active", cadence: "weekly", position: 1, subject: "", papers: 0, saved: 0, new: 0, new_saved: 0 });
const topics = [card("cot", "CoT monitoring", "watch"), card("interp", "Interpretability", "radar")];
const stuck = { paper_id: "2610.00009", failed_runs: 3, last_error: "invalid: not JSON", title: "Broken title" };
const quality = {
  night: { run: 1, error: null, splits: [], ungraded: [], awaiting_triage: [stuck], reviews_failed: [] },
  evaluation: null,
  calibration: [],
  matters_no_topic: [],
  correction_rate: [],
  collected: [],
};

function script(topicsReply: () => unknown = () => json(topics), page: unknown = quality) {
  return scriptFetch({
    "GET /api/status": () => json({ state: "attention", read: 1, awaiting_triage: 1 }),
    "GET /api/topics": topicsReply,
    "GET /api/settings/quality": () => json(page),
    "GET /api/settings/calibrate": () => json([]),
    "GET /api/papers/2610.00009": () => json({ id: "2610.00009", title: "Broken title", abstract: "Garbled abstract.", authors: [], categories: [], added: "2026-10-12T00:00:00Z", arxiv_url: "", pdf_url: "", topics: [], history: [], chosen: [] }),
    "PUT /api/settings/triage/2610.00009": () => json({ candidates: ["cot"] }),
    "POST /api/settings/triage/2610.00009/correction": () => new Response(null, { status: 204 }),
    "POST /api/settings/triage/2610.00009/dismiss": () => new Response(null, { status: 204 }),
  });
}

describe("a paper awaiting triage", () => {
  it("is triaged by hand with an answer for every active topic", async () => {
    const calls = script();
    const user = userEvent.setup();
    renderApp("/settings/quality");
    await user.click(await screen.findByRole("button", { name: "Triage by hand" }));
    await user.click(within(screen.getByRole("group", { name: "Could it belong to a Watch topic?" })).getByRole("button", { name: "CoT monitoring" }));
    await user.click(screen.getByRole("button", { name: "Send it on" }));
    await waitFor(() => expect(calls.find((c) => c.method === "PUT")).toBeTruthy());
    expect(calls.find((c) => c.method === "PUT")).toMatchObject({ url: "/api/settings/triage/2610.00009", body: { answers: { cot: true, interp: false } } });
  });

  it("cannot be sent on until the topics are known, and offers to try again", async () => {
    let failures = 0;
    const calls = script(() => {
      failures += 1;
      return failures === 1 ? json({ detail: "down" }, 500) : json(topics);
    });
    const user = userEvent.setup();
    renderApp("/settings/quality");
    await user.click(await screen.findByRole("button", { name: "Triage by hand" }));
    expect(await screen.findByText(/The topics could not be loaded/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Send it on" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "Try again" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Send it on" })).toBeEnabled());
    await user.click(screen.getByRole("button", { name: "Send it on" }));
    await waitFor(() => expect(calls.find((c) => c.method === "PUT")).toBeTruthy());
    expect(calls.find((c) => c.method === "PUT")!.body).toEqual({ answers: { cot: false, interp: false } });
  });

  it("sends only the text that changed when the owner fixes it", async () => {
    const calls = script();
    const user = userEvent.setup();
    renderApp("/settings/quality");
    await user.click(await screen.findByRole("button", { name: "Fix the text" }));
    const title = await screen.findByLabelText("Title");
    expect(screen.getByRole("button", { name: "Save and triage again" })).toBeDisabled();
    await user.clear(title);
    await user.type(title, "Fixed title");
    await user.click(screen.getByRole("button", { name: "Save and triage again" }));
    await waitFor(() => expect(calls.find((c) => c.url.endsWith("/correction"))).toBeTruthy());
    expect(calls.find((c) => c.url.endsWith("/correction"))!.body).toEqual({ title: "Fixed title", abstract: null });
  });

  it("asks twice before dismissing", async () => {
    const calls = script();
    const user = userEvent.setup();
    renderApp("/settings/quality");
    await user.click(await screen.findByRole("button", { name: "Dismiss" }));
    expect(calls.some((c) => c.url.endsWith("/dismiss"))).toBe(false);
    await user.click(screen.getByRole("button", { name: "Dismiss, never triage" }));
    await waitFor(() => expect(calls.some((c) => c.method === "POST" && c.url.endsWith("/dismiss"))).toBe(true));
  });
});

describe("a review that failed", () => {
  it("is listed on Quality instead of all quiet, with the next run's retry noted", async () => {
    const failed = { topic: "interp", topic_name: "Interpretability", kind: "radar", period: "week", day: "2026-10-05", error: "timed out" };
    script(undefined, { ...quality, night: { ...quality.night, awaiting_triage: [], reviews_failed: [failed] } });
    renderApp("/settings/quality");
    expect(await screen.findByText(/1 review failed/)).toBeInTheDocument();
    expect(screen.getByText(/review for the week of 5 October/)).toBeInTheDocument();
    expect(screen.getByText("timed out")).toBeInTheDocument();
    expect(screen.getByText(/the next run tries them again/)).toBeInTheDocument();
    expect(screen.queryByText(/All quiet/)).not.toBeInTheDocument();
  });
});
