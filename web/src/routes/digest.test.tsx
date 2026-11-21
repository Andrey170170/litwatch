import { InfiniteQueryObserver } from "@tanstack/react-query";
import { act, cleanup, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { historyQuery } from "@/api/queries";
import { addDays, dayLabel, isoDay, SPAN } from "@/lib/digests";
import { freshClient, json, renderApp, scriptFetch, viewport } from "@/test/harness";

const focus = { slug: "cotf", name: "CoT faithfulness", depth: "focus", monogram: "CF", colour: "#c026d3" };
const area = { slug: "interp", name: "Interpretability", depth: "area", monogram: "IN", colour: "#4f46e5" };
const status = { state: "quiet", papers: 212, awaiting_classification: 0, analysis_failed: 0, ungraded: 0, fulltext_failed: 0, digests_failed: 0 };
// The server's today is the clock's, as when the browser and the schedule share a zone.
const summary = { status, topics: [focus, area], settings: { waiting: 0, last_session: null, misses: 0, topics: 2, classes: 2, spent: 0, budget: null, failing: [] }, get today() { return isoDay(new Date()); } };
const counts = { papers: 2, picks: 0, central: 1, related: 1, must_read: 1 };
const paper = { id: "2610.00001", title: "Faithful traces", announced: "2026-10-14", role: "listed", why: null, revision: 1, gist: "Traces leave steps out.", fit: "central", grade: "must_read", rarity: "epic", set_aside: false };
const related = { ...paper, id: "2610.00002", title: "A related one", fit: "related", grade: "relevant", rarity: "rare", gist: "Related gist." };
const card = { id: 7, topic: focus, period: "day", day: "2026-10-14", state: "written", counts, best_rarity: "epic", gist: "Two measurement papers.", papers: [paper, related], more: 0 };
const quiet = { id: 8, topic: area, period: "day", day: "2026-10-14", state: "nothing_new", counts: { papers: 0, picks: 0, central: 0, related: 0, must_read: 0 }, best_rarity: null, gist: null, papers: [], more: 0 };
const digest = { ...card, text: "Two measurement papers, both about how much reaches the trace.\n\n[Faithful traces](2610.00001) checks statements causally.", error: null, written_at: "2026-10-15T03:00:00Z" };
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

  it("steps from the last day with digests to an empty today, where today's run stands in place of the cards", async () => {
    vi.setSystemTime(new Date(2026, 9, 19, 1));
    viewport(true);
    const night = { due: new Date(2026, 9, 19, 2).toISOString(), deadline: new Date(2026, 9, 19, 7, 30).toISOString(), papers: 0, total: null, error: null };
    scriptFetch({
      "GET /api/summary": summary,
      "GET /api/digests/days/2026-10-16": { ...day, day: "2026-10-16", previous: "2026-10-15", next: null },
      "GET /api/digests/days/2026-10-19": { ...day, day: "2026-10-19", released: false, digests: [], previous: "2026-10-16", next: null, run: { ...night, state: "waiting" } },
    });
    renderApp("/?day=2026-10-16");
    await userEvent.click(await screen.findByRole("link", { name: "Newer" }));
    const panel = await screen.findByRole("status");
    expect(within(panel).getByText("Standby")).toBeInTheDocument();
    expect(within(panel).getByText("run at 02:00")).toBeInTheDocument();
    expect(screen.queryByText("No papers released.")).toBeNull();
    expect(screen.getByText("Today")).toBeInTheDocument();
    expect(screen.getByLabelText("Newer")).toHaveAttribute("aria-disabled", "true");
  });

  it("reads today again every minute while its run goes, and shows the digests once it is done", async () => {
    vi.setSystemTime(new Date(2026, 9, 19, 3));
    viewport(true);
    const night = { due: new Date(2026, 9, 19, 2).toISOString(), deadline: new Date(2026, 9, 19, 7, 30).toISOString(), papers: 120, total: 480, error: null };
    const empty = { ...day, day: "2026-10-19", released: true, digests: [], previous: "2026-10-16", next: null, run: { ...night, state: "running" } };
    let today: object = empty;
    scriptFetch({ "GET /api/summary": summary, "GET /api/digests/days/2026-10-19": () => json(today) });
    renderApp("/");
    expect(await screen.findByText("120 / 480 papers read")).toBeInTheDocument();
    today = { ...empty, digests: [{ ...card, day: "2026-10-19" }], run: { ...night, state: "done" } };
    await vi.advanceTimersByTimeAsync(60_000);
    expect(await screen.findByText("Two measurement papers.")).toBeInTheDocument();
    expect(screen.queryByRole("status")).toBeNull();
  });

  // The browser's clock says Sunday evening; the schedule's zone, ahead of it, is already on Monday.
  const ahead = { ...summary, today: "2026-10-19" };

  it("opens on today in the schedule's zone, not the browser's, and always stops on it", async () => {
    vi.setSystemTime(new Date(2026, 9, 18, 20));
    viewport(true);
    scriptFetch({
      "GET /api/summary": ahead,
      "GET /api/digests/days/2026-10-19": { ...day, day: "2026-10-19", previous: "2026-10-16" },
      "GET /api/digests/days/2026-10-16": { ...day, day: "2026-10-16", next: null },
    });
    renderApp("/");
    expect(await screen.findByText("Mon 19 Oct")).toBeInTheDocument();
    expect(screen.getByText("Today")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("link", { name: "Older" }));
    // Nothing after the 16th has digests, as the server sees it: › still leads back to the schedule's today.
    await waitFor(() => expect(screen.getByRole("link", { name: "Newer" })).toHaveAttribute("href", "/?day=2026-10-19"));
  });

  it("waits for the schedule's today before it reads a Day, so a day that becomes today is read as today", async () => {
    // Just past the browser's midnight, the schedule's zone still on Sunday: Monday is not read before it is today.
    vi.setSystemTime(new Date(2026, 9, 19, 0, 5));
    viewport(true);
    let land: (s: object) => void = () => {};
    const first = new Promise<object>((resolve) => (land = resolve));
    let today = "2026-10-18";
    const calls = scriptFetch({
      "GET /api/summary": () => (calls.filter((c) => c.url === "/api/summary").length === 1 ? first : { ...summary, today }),
      "GET /api/digests/days/2026-10-18": { ...day, day: "2026-10-18", digests: [], previous: null },
      "GET /api/digests/days/2026-10-19": { ...day, day: "2026-10-19", released: false, digests: [], previous: "2026-10-18", run: { state: "waiting", due: new Date(2026, 9, 19, 2).toISOString(), deadline: new Date(2026, 9, 19, 7, 30).toISOString(), papers: 0, total: null, error: null } },
    });
    renderApp("/");
    expect(await screen.findByText("Loading…")).toBeInTheDocument();
    land({ ...summary, today });
    expect(await screen.findByText("Sun 18 Oct")).toBeInTheDocument();
    expect(calls.some((c) => c.url.startsWith("/api/digests/days/2026-10-19"))).toBe(false);
    // The schedule's midnight: the next poll moves today, and Monday is read as today, with its run.
    today = "2026-10-19";
    await vi.advanceTimersByTimeAsync(60_000);
    expect(await screen.findByText("Mon 19 Oct")).toBeInTheDocument();
    expect(within(await screen.findByRole("status")).getByText("Standby")).toBeInTheDocument();
  });

  it("says when today is unknown because the summary failed, and tries again", async () => {
    viewport(true);
    let down = true;
    scriptFetch({ "GET /api/summary": () => (down ? json({ detail: "down" }, 500) : summary), "GET /api/digests/days/2026-10-14": day });
    renderApp("/");
    expect(await screen.findByText(/Could not load today/)).toBeInTheDocument();
    down = false;
    await userEvent.click(screen.getAllByRole("button", { name: "Try again" })[0]!);
    expect(await screen.findByText("2 new · 1 must read")).toBeInTheDocument();
  });

  it("keeps History's older pages loaded when the schedule's midnight moves today, and adds the new day", async () => {
    vi.setSystemTime(new Date(2026, 9, 18, 22));
    viewport(true);
    let today = "2026-10-18";
    const calls = scriptFetch({
      "GET /api/summary": () => ({ ...summary, today }),
      "GET /api/digests": ({ url }) => {
        const until = new URLSearchParams(url.split("?")[1]).get("until")!;
        return { days: Array.from({ length: SPAN + 1 }, (_, i) => ({ day: addDays(until, -i), released: true, marks: [] })), weeks: [], start: "2026-01-05" };
      },
    });
    const client = freshClient();
    renderApp("/history", client);
    expect((await screen.findByRole("link", { name: "Sun 18 Oct" })).closest(".grp")).toHaveTextContent("today");
    // The reader scrolls on to an older page.
    const older = addDays("2026-10-18", -SPAN - 1);
    await act(() => new InfiniteQueryObserver(client, historyQuery("2026-10-18", SPAN)).fetchNextPage());
    expect(await screen.findByRole("link", { name: dayLabel(older) })).toBeInTheDocument();
    today = "2026-10-19";
    await vi.advanceTimersByTimeAsync(60_000);
    expect((await screen.findByRole("link", { name: "Mon 19 Oct" })).closest(".grp")).toHaveTextContent("today");
    expect(screen.getByRole("link", { name: "Sun 18 Oct" }).closest(".grp")).not.toHaveTextContent("today");
    expect(screen.getByRole("link", { name: dayLabel(older) })).toBeInTheDocument();
    // Both pages read again, the older one from the newer's end.
    const untils = calls.filter((c) => c.url.startsWith("/api/digests?")).map((c) => new URLSearchParams(c.url.split("?")[1]).get("until"));
    expect(untils).toEqual(["2026-10-18", addDays("2026-10-18", -SPAN - 1), "2026-10-19", addDays("2026-10-19", -SPAN - 1)]);
  });

  it("takes Week's last week and History's pinned week and today from the schedule's today", async () => {
    vi.setSystemTime(new Date(2026, 9, 18, 20));
    viewport(true);
    const mark = (id: number, d: string, period = "day") => ({ id, topic: focus, period, day: d, state: "written", counts, best_rarity: "rare" });
    const days = ["2026-10-19", "2026-10-18", "2026-10-17", "2026-10-16"].map((d, i) => ({ day: d, released: i === 0 || i === 3, marks: i === 0 || i === 3 ? [mark(i, d)] : [] }));
    const calls = scriptFetch({
      "GET /api/summary": ahead,
      "GET /api/digests/weeks/2026-10-12": { monday: "2026-10-12", digests: [], previous: "2026-10-05", next: null },
      "GET /api/digests": { days, weeks: [{ monday: "2026-10-12", marks: [mark(50, "2026-10-12", "week")] }], start: "2026-10-16" },
    });
    renderApp("/week");
    expect(await screen.findByText("Last week")).toBeInTheDocument();
    expect(screen.getByText("12–18 Oct")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("link", { name: "History" }));
    expect(await screen.findByText("This week")).toBeInTheDocument();
    expect(screen.getByText("weekly digests · 12–18 Oct")).toBeInTheDocument();
    expect(screen.getByText("today").closest(".grp")).toHaveTextContent("Mon 19 Oct");
    expect(calls.filter((c) => c.url.startsWith("/api/digests?")).map((c) => new URLSearchParams(c.url.split("?")[1]).get("until"))).toContain("2026-10-19");
    await userEvent.click(screen.getByRole("link", { name: "Calendar" }));
    await waitFor(() => expect(document.querySelector(".mday.today .mnum")).toHaveTextContent("19"));
  });

  it("opens a digest over the day on the desktop, its papers by role, and closes it back to the day", async () => {
    viewport(true);
    scriptFetch({ "GET /api/summary": summary, "GET /api/digests/days/2026-10-14": day, "GET /api/digests/7": digest });
    const { router } = renderApp("/");
    await userEvent.click(await screen.findByText("Two measurement papers."));
    const over = await screen.findByRole("dialog");
    expect(router.state.location.href).toContain("digest=7");
    expect(within(over).getByText(/Daily digest · Wed 14 Oct/)).toBeInTheDocument();
    // A paper the text names shows as its name, linking to its page under the digest's topic.
    const named = within(over).getByRole("link", { name: "Faithful traces" });
    expect(named).toHaveAttribute("href", "/paper/2610.00001?topic=cotf");
    expect(named.closest("p")).toHaveTextContent(/^Faithful traces checks statements causally\.$/);
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

  it("folds a digest's updated papers under its papers, opens them for the session and closes them, each row one line with its version", async () => {
    viewport(true);
    const updated = (n: number, over: object = {}) => ({ ...paper, id: `2605.0000${n}`, title: `Updated ${n}`, announced: "2026-05-12", role: "updated", version: 3, gist: `Gist ${n}.`, ...over });
    const withUpdated = { ...digest, counts: { ...counts, updated: 2 }, papers: [paper, related, updated(1), updated(2, { rarity: "rare" })] };
    scriptFetch({ "GET /api/summary": summary, "GET /api/digests/7": withUpdated });
    renderApp("/digests/7");
    const head = await screen.findByRole("button", { name: "Updated · 2" });
    expect(screen.getByText(/2 new · 1 must read · 2 updated/)).toBeInTheDocument();
    expect(head).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByText("Updated 1")).toBeNull();
    // last, under the digest's papers
    expect(screen.getByText("Related").compareDocumentPosition(head)).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
    await userEvent.click(head);
    expect(head).toHaveAttribute("aria-expanded", "true");
    const rows = within(screen.getByRole("region", { name: "Updated" })).getAllByRole("link");
    expect(rows.map((r) => r.textContent)).toEqual(["Updated 1v3 · first out May 2026", "Updated 2v3 · first out May 2026"]);
    expect(rows[1]).toHaveStyle({ "--rc": "var(--r-rare)" });
    expect(screen.queryByText("Gist 1.")).toBeNull();
    // the way back to the digest finds the list as it was left
    cleanup();
    renderApp("/digests/7");
    const again = await screen.findByRole("button", { name: "Updated · 2" });
    expect(again).toHaveAttribute("aria-expanded", "true");
    await userEvent.click(again);
    expect(again).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByText("Updated 1")).toBeNull();
  });

  it("shows a quiet digest's updated papers under its one line, and its card's count", async () => {
    viewport(true);
    const up = { ...paper, id: "2605.00009", title: "An old paper again", announced: "2026-04-02", role: "updated", version: 2 };
    const quietUp = { ...quiet, counts: { ...quiet.counts, updated: 3 } };
    scriptFetch({
      "GET /api/summary": summary,
      "GET /api/digests/days/2026-10-14": { ...day, digests: [{ ...card, counts: { ...counts, updated: 1 } }, quietUp] },
      "GET /api/digests/8": { ...quietUp, text: null, error: null, written_at: null, papers: [up] },
    });
    renderApp("/");
    expect(await screen.findByText("2 new · 1 must read · 1 updated")).toBeInTheDocument();
    await userEvent.click(screen.getByText("Nothing new · 3 updated"));
    const over = await screen.findByRole("dialog");
    expect(within(over).getByText(/No new paper was due/)).toBeInTheDocument();
    await userEvent.click(within(over).getByRole("button", { name: "Updated · 1" }));
    expect(within(over).getByRole("link", { name: /An old paper again/ })).toHaveTextContent("v2 · first out Apr 2026");
  });

  it("on the phone opens a digest as its own page, and puts the stepper in the bottom bar", async () => {
    viewport(false);
    scriptFetch({ "GET /api/summary": summary, "GET /api/digests/days/2026-10-14": day, "GET /api/digests/7": digest });
    const { router } = renderApp("/");
    await screen.findByText("Two measurement papers.");
    const foot = document.querySelector(".pfoot .stp.foot");
    expect(foot).not.toBeNull();
    expect(within(foot as HTMLElement).getByText("Today")).toBeInTheDocument();
    expect(document.querySelector("main.screen")).not.toBeNull();
    await userEvent.click(screen.getByText("Two measurement papers."));
    await waitFor(() => expect(router.state.location.pathname).toBe("/digests/7"));
    expect(await screen.findByRole("link", { name: "Faithful traces" })).toBeInTheDocument();
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
    scriptFetch({ "GET /api/summary": summary, "GET /api/digests/days/2026-10-14": () => (down ? json({ detail: "down" }, 500) : day) });
    renderApp("/");
    expect(await screen.findByText(/Could not load the day/)).toBeInTheDocument();
    down = false;
    await userEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByText("Two measurement papers.")).toBeInTheDocument();
  });
});
