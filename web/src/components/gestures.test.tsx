import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { renderApp, scriptFetch, viewport } from "@/test/harness";

const focus = { slug: "cotf", name: "CoT faithfulness", depth: "focus", monogram: "CF", colour: "#c026d3" };
const status = { state: "quiet", papers: 212, awaiting_classification: 0, analysis_failed: 0, ungraded: 0, fulltext_failed: 0, digests_failed: 0 };
const summary = { status, topics: [focus], settings: { waiting: 0, last_session: null, misses: 0, topics: 1, classes: 1, spent: 0, budget: null, failing: [] } };
const counts = { papers: 1, picks: 0, central: 1, related: 0, must_read: 1 };
const paper = { id: "2610.00001", title: "Faithful traces", announced: "2026-10-14", role: "listed", why: null, revision: 1, gist: "Traces leave steps out.", fit: "central", grade: "must_read", rarity: "epic", set_aside: false };
const card = { id: 7, topic: focus, period: "day", day: "2026-10-14", state: "written", counts, best_rarity: "epic", gist: "One paper.", papers: [paper], more: 0 };
const full = (id: string, extra: object) => ({
  id,
  title: "Faithful traces",
  authors: ["A. Author"],
  abstract: "We study things.",
  published: "2026-10-13",
  version: 1,
  versions: [{ version: 1, date: "2026-10-13" }],
  revision: 1,
  arxiv_url: `https://arxiv.org/abs/${id}`,
  pdf_url: `https://arxiv.org/pdf/${id}`,
  source_url: `https://arxiv.org/e-print/${id}`,
  rarity: null,
  badges: [],
  gist: null,
  weight: null,
  facets: null,
  saved: false,
  topics: [],
  states: [],
  publication_by_owner: false,
  card_requestable: false,
  ...extra,
});
const papers = { rows: [], total: 0, counts: { legendary: 0, epic: 0, rare: 0, uncommon: 0, common: 0, unrated: 0 }, grades: { must_read: 0, relevant: 0, adjacent: 0, none: 0, ungraded: 0 }, outside: [] };
const day = (d: string, previous: string | null, next: string | null) => ({ day: d, released: true, digests: [{ ...card, day: d }], previous, next, weekly: null });
const routes = {
  "GET /api/summary": summary,
  "GET /api/status": status,
  "GET /api/digests/days/2026-10-14": day("2026-10-14", "2026-10-13", null),
  "GET /api/digests/days/2026-10-13": day("2026-10-13", null, "2026-10-14"),
  "GET /api/topics": [{ depth: "focus", topics: [focus] }],
};

/** A finger: down at (x, y), moves through `path` with `ms` between moves (300 is a slow drag the distance decides;
 * 20 a flick), lifts. Event times are the faked clock's, so the velocity read is the one meant. */
function swipe(target: Element, from: [number, number], path: [number, number][], { lift = true, ms = 300 } = {}) {
  const init = (x: number, y: number) => ({ clientX: x, clientY: y, pointerId: 1, pointerType: "touch", isPrimary: true, button: 0, buttons: 1 });
  fireEvent.pointerDown(target, init(...from));
  for (const [x, y] of path) {
    vi.advanceTimersByTime(ms);
    fireEvent.pointerMove(window, init(x, y));
  }
  if (lift) fireEvent.pointerUp(window, init(...(path.at(-1) ?? from)));
}

describe("The phone's gestures", () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true, now: new Date(2026, 9, 14, 9) });
    viewport(false, true); // reduced motion: a settle takes no time
    Object.defineProperty(window, "innerWidth", { value: 390, configurable: true });
  });
  afterEach(() => {
    // a drag's swallow is for the click its release makes; a test's ends with it (the next test's clock starts over)
    document.dispatchEvent(new MouseEvent("pointerdown"));
    vi.useRealTimers();
  });

  it("steps the Day to the older one on a swipe to the right, as a history entry, and not where nothing lies", async () => {
    scriptFetch(routes);
    const { router } = renderApp("/");
    await screen.findByText("Today");
    const length = router.history.length;
    // to the left there is no newer day: the content resists and nothing happens
    swipe(screen.getByText("One paper."), [300, 300], [[290, 302], [150, 305]]);
    await waitFor(() => expect(document.querySelector("main")?.style.transform).toBe(""));
    expect(router.state.location.search).toEqual({});
    // to the right, past a quarter of the width: the older day
    swipe(screen.getByText("One paper."), [100, 300], [[110, 302], [220, 305]]);
    await waitFor(() => expect(screen.queryByText("Today")).toBeNull());
    expect(router.state.location.search).toEqual({ day: "2026-10-13" });
    expect(router.history.length).toBe(length + 1);
    expect(document.querySelector("main")?.dataset.came).toBe("prev");
    router.history.back();
    await waitFor(() => expect(screen.getByText("Today")).toBeInTheDocument());
  });

  it("leaves a drag that starts vertical to the page, and a short one alone", async () => {
    scriptFetch(routes);
    const { router } = renderApp("/");
    await screen.findByText("Today");
    swipe(screen.getByText("One paper."), [100, 300], [[104, 312], [200, 420]]);
    // the finger went on sideways after the page took the drag: the content stayed where it was
    expect(document.querySelector("main")?.style.transform).toBe("");
    swipe(screen.getByText("One paper."), [100, 300], [[160, 300]]);
    await Promise.resolve();
    expect(router.state.location.search).toEqual({});
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("opens the drawer from the left edge on a top-level screen, following the finger, and shuts it when let go early", async () => {
    scriptFetch(routes);
    renderApp("/");
    await screen.findByText("Today");
    swipe(screen.getByRole("main"), [10, 300], [[30, 300], [200, 300]], { lift: false });
    const drawer = await screen.findByRole("dialog", { name: "Menu" });
    expect(drawer).toHaveAttribute("data-drag", "opening");
    // the drawer (300 wide in this viewport) has come in by the finger's 190 px
    expect(drawer.style.transform).toBe("translateX(-110px)");
    fireEvent.pointerUp(window, { clientX: 200, clientY: 300, pointerId: 1, isPrimary: true });
    await waitFor(() => expect(drawer).not.toHaveAttribute("data-drag"));
    expect(screen.getByRole("link", { name: "Papers" })).toBeInTheDocument();
    // what the drag wrote is gone; what Radix keeps inline (the dialog takes pointer events while the body does not) stays
    expect(drawer.style.transform).toBe("");
    expect(drawer.style.pointerEvents).toBe("auto");
    // dragged shut from the open drawer, past half its width
    swipe(screen.getByRole("link", { name: "Papers" }), [250, 300], [[240, 300], [40, 300]]);
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    // let go a few px in: it shuts again
    swipe(screen.getByRole("main"), [10, 300], [[30, 300], [60, 300]]);
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  });

  it("goes back from the left edge on a deeper screen, past a third of the width, and stays on a short drag", async () => {
    scriptFetch({ ...routes, "GET /api/papers/2610.00001": full("2610.00001", { in_collection: true }) });
    const { router } = renderApp("/");
    await screen.findByText("Today");
    fireEvent.click(screen.getByText("Faithful traces"));
    await screen.findByRole("button", { name: "Back" });
    const length = router.history.length;
    swipe(screen.getByRole("main"), [8, 300], [[30, 300], [80, 300]], { lift: false });
    const under = document.querySelector<HTMLElement>(".under")!;
    expect(under.hidden).toBe(false);
    fireEvent.pointerUp(window, { clientX: 80, clientY: 300, pointerId: 1, isPrimary: true });
    await waitFor(() => expect(document.querySelector(".page")?.getAttribute("style") ?? "").toBe(""));
    expect(router.state.location.pathname).toBe("/paper/2610.00001");
    // the copy of the screen under is gone with the drag: the router must find the live screen to restore its scroll
    expect(under.hidden).toBe(true);
    expect(under.childElementCount).toBe(0);
    // a flick from the edge goes back even from a short way in
    swipe(screen.getByRole("main"), [8, 300], [[30, 300], [70, 300]], { ms: 20 });
    await waitFor(() => expect(router.state.location.pathname).toBe("/"));
    expect(router.history.length).toBe(length);
    await screen.findByText("Today");
    expect(document.querySelector("main")?.dataset.came).toBe("still");
  });

  it("ends a drag when the screen changes under the finger, and keeps a stepped-away screen's copy at rest", async () => {
    const second = { ...paper, id: "2610.00002", title: "Second traces" };
    scriptFetch({
      ...routes,
      "GET /api/digests/days/2026-10-14": { ...day("2026-10-14", "2026-10-13", null), digests: [{ ...card, papers: [paper, second] }] },
      "GET /api/papers": papers,
      "GET /api/papers/2610.00001": full("2610.00001", { in_collection: true }),
      "GET /api/papers/2610.00002": full("2610.00002", { in_collection: true, title: "Second traces" }),
    });
    const { router } = renderApp("/");
    await screen.findByText("Today");
    // a step drag in progress when a navigation comes: what it wrote is taken back, and its release does nothing
    swipe(screen.getByText("One paper."), [100, 300], [[110, 300], [220, 300]], { lift: false });
    expect(document.querySelector("main")?.style.transform).toBe("translateX(120px)");
    await router.navigate({ to: "/papers" });
    await waitFor(() => expect(router.state.location.pathname).toBe("/papers"));
    expect(document.querySelector("main")?.style.transform).toBe("");
    fireEvent.pointerUp(window, { clientX: 220, clientY: 300, pointerId: 1, isPrimary: true });
    await Promise.resolve();
    expect(router.state.location.pathname).toBe("/papers");
    // a finger that landed but had not moved far enough to mean a drag is let go of too
    await router.navigate({ to: "/" });
    await screen.findByText("Today");
    fireEvent.pointerDown(screen.getByText("One paper."), { clientX: 100, clientY: 300, pointerId: 1, pointerType: "touch", isPrimary: true, button: 0 });
    await router.navigate({ to: "/papers" });
    await waitFor(() => expect(router.state.location.pathname).toBe("/papers"));
    swipe(document.body, [100, 300], [[110, 300], [220, 300]]);
    await Promise.resolve();
    expect(router.state.location.pathname).toBe("/papers");
    expect(document.querySelector("main")?.style.transform).toBe("");
    // a paper stepped away from is copied for the way back as it was, not as it left (faded, slid)
    await router.navigate({ to: "/" });
    await screen.findByText("Today");
    fireEvent.click(screen.getByText("Faithful traces"));
    await screen.findByRole("button", { name: "Back" });
    swipe(screen.getByRole("main"), [300, 300], [[290, 300], [150, 300]]);
    await waitFor(() => expect(router.state.location.pathname).toBe("/paper/2610.00002"));
    await screen.findByRole("heading", { name: "Second traces" });
    swipe(screen.getByRole("main"), [8, 300], [[30, 300], [80, 300]], { lift: false });
    const copy = document.querySelector<HTMLElement>(".under .screen")!;
    expect(copy.style.opacity).toBe("");
    expect(copy.style.transform).toBe("");
    expect(copy.hasAttribute("data-scroll-restoration-id")).toBe(false);
    fireEvent.pointerUp(window, { clientX: 80, clientY: 300, pointerId: 1, isPrimary: true });
  });

  it("puts the content back when the page refuses the step: Calibrate, while a save is in flight", async () => {
    const item = (position: number) => ({ position, paper_id: `2610.0000${position}`, topic: "cotf", why: "above", verdict: "related", answer: null, answered_at: null, title: `Drawn ${position}`, abstract: "A.", authors: ["A"], published: "2026-10-13", fit: {}, appraisal: null });
    let release!: () => void;
    const calls = scriptFetch({
      ...routes,
      "GET /api/settings/calibrate/1": { id: 1, seed: 7, drawn_at: "2026-10-15T10:00:00Z", items: [item(1), item(2)] },
      "PUT /api/settings/calibrate/1/items/1": () => new Promise<Response>((r) => (release = () => r(new Response(null, { status: 204 })))),
    });
    const { router } = renderApp("/settings/calibrate/1?at=1");
    await screen.findByText("Drawn 1");
    fireEvent.click(within(await screen.findByRole("group", { name: "Fit for cotf" })).getByRole("button", { name: "related" }));
    // the swipe to the next paper records the answer first; nothing moves while that save is in flight
    swipe(screen.getByText("Drawn 1"), [300, 300], [[290, 300], [150, 300]]);
    await waitFor(() => expect(calls.filter((c) => c.method === "PUT").length).toBe(1));
    await waitFor(() => expect(document.querySelector("main")?.style.opacity).toBe(""));
    expect(screen.getByText("Drawn 1")).toBeInTheDocument();
    expect(router.state.location.href).toBe("/settings/calibrate/1?at=1");
    release();
    await waitFor(() => expect(router.state.location.href).toBe("/settings/calibrate/1?at=2"));
  });
});

describe("A sheet", () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    viewport(false, true);
  });
  afterEach(() => vi.useRealTimers());
  it("closes when dragged down past a third of its height by the handle, and springs back from less", async () => {
    scriptFetch({ ...routes, "GET /api/papers": papers });
    renderApp("/papers");
    fireEvent.click(await screen.findByRole("button", { name: "Filters" }));
    const sheet = await screen.findByRole("dialog", { name: "Filters" });
    Object.defineProperty(sheet, "offsetHeight", { value: 300 });
    const handle = sheet.querySelector(".hdl")!;
    swipe(handle, [195, 500], [[195, 512], [195, 560]]);
    await waitFor(() => expect(sheet.style.transform).toBe(""));
    expect(screen.getByRole("dialog", { name: "Filters" })).toBeInTheDocument();
    expect(sheet.style.pointerEvents).toBe("auto");
    swipe(handle, [195, 500], [[195, 512], [195, 620]]);
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    // a short flick down closes it too (the tap comes after the drag's release has had its 350 ms)
    vi.advanceTimersByTime(400);
    fireEvent.click(screen.getByRole("button", { name: "Filters" }));
    const again = await screen.findByRole("dialog", { name: "Filters" });
    Object.defineProperty(again, "offsetHeight", { value: 300 });
    swipe(again.querySelector(".hdl")!, [195, 500], [[195, 512], [195, 540]], { ms: 20 });
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  });
});

describe("The share target", () => {
  it("lands on the shared paper with Report as a miss open, and searches a share with no id", async () => {
    viewport(false);
    scriptFetch({
      ...routes,
      "GET /api/papers/2401.12345": full("2401.12345", { title: "Shared", in_collection: false, stored: false }),
      "GET /api/papers": papers,
    });
    const { router } = renderApp("/share?url=https%3A%2F%2Farxiv.org%2Fabs%2F2401.12345v2&title=Shared");
    await waitFor(() => expect(router.state.location.pathname).toBe("/paper/2401.12345"));
    expect(await screen.findByRole("group", { name: "Report as a miss" })).toBeInTheDocument();
    await router.navigate({ to: "/share", search: { title: "attention is all you need" } });
    await waitFor(() => expect(router.state.location.pathname).toBe("/papers"));
    expect(router.state.location.search).toEqual({ q: "attention is all you need" });
  });
});
