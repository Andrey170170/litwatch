import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { json, renderApp, scriptFetch, viewport } from "@/test/harness";

const focus = { slug: "cotf", name: "CoT faithfulness", depth: "focus", monogram: "CF", colour: "#c026d3" };
const area = { slug: "interp", name: "Interpretability", depth: "area", monogram: "IN", colour: "#4f46e5" };
const status = { state: "quiet", papers: 212, awaiting_classification: 0, analysis_failed: 0, ungraded: 0, fulltext_failed: 0, digests_failed: 0 };
const summary = { status, topics: [focus, area], settings: { waiting: 0, last_session: null, misses: 0, topics: 2, classes: 2, spent: 0, budget: null, failing: [] } };
const counts = { papers: 2, picks: 0, central: 1, related: 1, must_read: 1 };
const paper = { id: "2610.00001", title: "Faithful traces", announced: "2026-10-14", role: "listed", why: null, revision: 1, gist: "Traces leave steps out.", fit: "central", grade: "must_read", rarity: "epic", set_aside: false };
const related = { ...paper, id: "2610.00002", title: "A related one", fit: "related", grade: "relevant", rarity: "rare", gist: "Related gist." };
const card = { id: 7, topic: focus, period: "day", day: "2026-10-14", state: "written", counts, best_rarity: "epic", gist: "Two measurement papers.", papers: [paper, related], more: 0 };
const quiet = { id: 8, topic: area, period: "day", day: "2026-10-14", state: "nothing_new", counts: { papers: 0, picks: 0, central: 0, related: 0, must_read: 0 }, best_rarity: null, gist: null, papers: [], more: 0 };
const digest = { ...card, text: "Two measurement papers, both about how much reaches the trace.\n\nThe second checks statements causally.", error: null, written_at: "2026-10-15T03:00:00Z" };
const day = { day: "2026-10-14", released: true, digests: [card, quiet], previous: "2026-10-13", next: null, weekly: null };

describe("the Digest", () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true, now: new Date(2026, 9, 14, 9) });
  });
  afterEach(() => vi.useRealTimers());

  it("shows today's cards with their counts in the rarity colour, and steps to the day before", async () => {
    viewport(true);
    const calls = scriptFetch({
      "GET /api/summary": summary,
      "GET /api/status": status,
      "GET /api/digests/days/2026-10-14": day,
      "GET /api/digests/days/2026-10-13": { ...day, day: "2026-10-13", digests: [{ ...card, id: 5, day: "2026-10-13" }], previous: null, next: "2026-10-14" },
    });
    renderApp("/");
    expect(await screen.findByText("2 new · 1 must read")).toHaveStyle({ color: "var(--r-epic)" });
    expect(screen.getByText("Nothing new")).toBeInTheDocument();
    expect(screen.getByText("Two measurement papers.")).toBeInTheDocument();
    expect(screen.getByText("Today")).toBeInTheDocument();
    expect(screen.getByLabelText("Newer")).toHaveAttribute("aria-disabled", "true");
    await userEvent.click(screen.getByRole("link", { name: "Older" }));
    await waitFor(() => expect(screen.queryByText("Today")).toBeNull());
    expect(calls.filter((c) => c.url.startsWith("/api/digests/days/2026-10-13"))).toHaveLength(1);
    expect(screen.getByText(/Tue 13 Oct/)).toBeInTheDocument();
  });

  it("opens a digest over the day on the desktop, its papers by role, and closes it back to the day", async () => {
    viewport(true);
    scriptFetch({ "GET /api/summary": summary, "GET /api/status": status, "GET /api/digests/days/2026-10-14": day, "GET /api/digests/7": digest });
    const { router } = renderApp("/");
    await userEvent.click(await screen.findByText("Two measurement papers."));
    const over = await screen.findByRole("dialog");
    expect(router.state.location.href).toContain("digest=7");
    expect(within(over).getByText(/Daily digest · Wed 14 Oct/)).toBeInTheDocument();
    expect(within(over).getByText("The second checks statements causally.")).toBeInTheDocument();
    const central = within(over).getByText("Central");
    const relatedHead = within(over).getByText("Related");
    expect(central.compareDocumentPosition(relatedHead)).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
    // Related rows are one line: no Gist.
    expect(within(over).getByText("Traces leave steps out.")).toBeInTheDocument();
    expect(within(over).queryByText("Related gist.")).toBeNull();
    await userEvent.click(within(over).getByRole("button", { name: "‹ Close" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(router.state.location.href).not.toContain("digest");
    expect(screen.getByText("Two measurement papers.")).toBeInTheDocument();
  });

  it("on the phone opens a digest as its own page, and puts the stepper in the bottom bar", async () => {
    viewport(false);
    scriptFetch({ "GET /api/summary": summary, "GET /api/status": status, "GET /api/digests/days/2026-10-14": day, "GET /api/digests/7": digest });
    const { router } = renderApp("/");
    await screen.findByText("Two measurement papers.");
    const foot = document.querySelector(".pfoot .stp.foot");
    expect(foot).not.toBeNull();
    expect(within(foot as HTMLElement).getByText("Today")).toBeInTheDocument();
    expect(document.querySelector("main.screen")).not.toBeNull();
    await userEvent.click(screen.getByText("Two measurement papers."));
    await waitFor(() => expect(router.state.location.pathname).toBe("/digests/7"));
    expect(await screen.findByText("The second checks statements causally.")).toBeInTheDocument();
    expect(document.querySelector(".pfoot .stp")).toBeNull();
  });

  it("lists History newest first, a weekend as one line, this week's weeklies pinned, and says where the archive starts", async () => {
    viewport(true);
    const mark = (id: number, d: string, topic = focus) => ({ id, topic, period: "day", day: d, state: "written", counts, best_rarity: "rare" });
    const days = [];
    for (let n = 14; n >= 5; n--) {
      const d = `2026-10-${String(n).padStart(2, "0")}`;
      const weekend = n === 10 || n === 11;
      days.push({ day: d, released: !weekend, marks: weekend ? [] : [mark(n, d), { ...mark(100 + n, d, area), state: "nothing_new", best_rarity: null }] });
    }
    scriptFetch({
      "GET /api/summary": summary,
      "GET /api/status": status,
      "GET /api/digests": { days, weeks: [{ monday: "2026-10-05", marks: [{ ...mark(50, "2026-10-05"), period: "week" }] }, { monday: "2026-10-12", marks: [] }], start: "2026-10-05" },
    });
    renderApp("/history");
    expect(await screen.findByText("This week")).toBeInTheDocument();
    expect(screen.getByText("weekly digests · 5–11 Oct")).toBeInTheDocument();
    expect(screen.getByText("Week · 2 papers")).toBeInTheDocument();
    const heads = screen.getAllByText(/^(Wed 14 Oct|Tue 13 Oct|Mon 12 Oct|Sat – Sun 11 Oct|Fri 9 Oct)$/).map((e) => e.textContent);
    expect(heads).toEqual(["Wed 14 Oct", "Tue 13 Oct", "Mon 12 Oct", "Sat – Sun 11 Oct", "Fri 9 Oct"]);
    expect(screen.getByText("today")).toBeInTheDocument();
    expect(screen.getByText("The archive starts on Mon 5 Oct.")).toBeInTheDocument();
    // The calendar: a row per week, the week's weeklies in the W cell, today's cell marked.
    await userEvent.click(screen.getByRole("link", { name: "Calendar" }));
    expect(await screen.findByText("W 5–11 Oct")).toBeInTheDocument();
    // one current mode and one current view, whatever the search says
    expect(screen.getByRole("link", { name: "Calendar" })).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("link", { name: "List" })).not.toHaveAttribute("aria-current");
    expect(screen.getByRole("link", { name: "History" })).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("link", { name: "Day" })).not.toHaveAttribute("aria-current");
    expect(document.querySelector(".mday.today .mnum")).toHaveTextContent("14");
    expect(document.querySelectorAll(".mday.empty").length).toBeGreaterThan(0);
  });

  it("says a failed read of the day, and tries again", async () => {
    viewport(true);
    let down = true;
    scriptFetch({ "GET /api/summary": summary, "GET /api/status": status, "GET /api/digests/days/2026-10-14": () => (down ? json({ detail: "down" }, 500) : day) });
    renderApp("/");
    expect(await screen.findByText(/Could not load the day/)).toBeInTheDocument();
    down = false;
    await userEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByText("Two measurement papers.")).toBeInTheDocument();
  });
});
