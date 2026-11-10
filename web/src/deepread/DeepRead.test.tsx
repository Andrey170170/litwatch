import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { renderApp, scriptFetch, viewport } from "@/test/harness";
import icl from "./fixtures/read-icl.json";
import scarce from "./fixtures/read-scarce.json";

const agents = { slug: "agents", name: "Agents and tool use", depth: "field", monogram: "AG", colour: "#ca8a04" };
const topic = (t: typeof agents) => ({ topic: t, fit: "central", grade: "must_read", rarity: "uncommon", verdict: { fit: "central", reason: "why", split: false, escalated: false, opinions: {} }, missed: false });
const paper = {
  id: "2610.21029",
  title: "SCARCE",
  authors: ["A. Kumar"],
  abstract: "We propose SCARCE.",
  published: "2026-10-15",
  version: 2,
  versions: [{ version: 2, date: "2026-10-20" }],
  revision: 1,
  arxiv_url: "https://arxiv.org/abs/2610.21029",
  pdf_url: "https://arxiv.org/pdf/2610.21029",
  source_url: "https://arxiv.org/e-print/2610.21029",
  in_collection: true,
  rarity: "uncommon",
  badges: [{ ...agents, grade: "must_read", rarity: "uncommon" }],
  gist: { text: "The gist." },
  weight: { weight: "solid", reason: "r", source: "analysis" },
  facets: { contribution: "benchmark", releases_code: true, releases_weights: false },
  saved: false,
  topics: [topic(agents)],
  states: [],
  publication_by_owner: false,
  card_requestable: true,
};
const status = { state: "quiet", papers: 1, awaiting_classification: 0, analysis_failed: 0, ungraded: 0, fulltext_failed: 0, digests_failed: 0 };
const among = [{ id: "2610.31985", title: "The Helpful Agent Problem", relation: "agrees", note: "Both see cooperation exceed what operators intended.", rarity: "rare", in_collection: true }];
const written = { model: "gpt-6.1-sol", written_at: "2026-11-09T04:12:00Z", revision: 1, arxiv_version: 2, source: "latex" };
const withRead = { ...paper, card: { card: scarce, ...written, among } };
const oldCard = { card: { key_claim: "A meta-verifier catches 71% of unfaithful chains.", summary: "SIFT trains a probe.", limitations: ["Two models only."] }, ...written, among: [] };

/** The observers the page made, so a test can say what is in view: jsdom lays nothing out. */
class Observer {
  static all: Observer[] = [];
  watched = new Set<Element>();
  constructor(
    private callback: (entries: Partial<IntersectionObserverEntry>[]) => void,
    public options?: IntersectionObserverInit,
  ) {
    Observer.all.push(this);
  }
  observe(el: Element) {
    this.watched.add(el);
  }
  unobserve(el: Element) {
    this.watched.delete(el);
  }
  disconnect() {
    this.watched.clear();
  }
  see(el: Element, isIntersecting: boolean) {
    act(() => this.callback([{ target: el, isIntersecting, boundingClientRect: { top: 0 } as DOMRectReadOnly }]));
  }
}
/** Scroll a section's rung into, or out of, the band the contents watch; `.dread` for the read as a whole. */
function see(selector: string, inView = true) {
  const el = document.querySelector(selector)!;
  const watching = Observer.all.filter((o) => o.watched.has(el));
  expect(watching).toHaveLength(1);
  watching[0]!.see(el, inView);
}
const scrolled = vi.fn();

beforeEach(() => {
  Observer.all = [];
  vi.stubGlobal("IntersectionObserver", Observer);
  scrolled.mockClear();
  Element.prototype.scrollIntoView = function (this: Element) {
    scrolled(this.id);
  };
});

async function open(card: object = withRead, desktop = true) {
  viewport(desktop);
  const calls = scriptFetch({ "GET /api/status": status, "GET /api/topics": [], [`GET /api/papers/${paper.id}`]: card, [`POST /api/papers/${paper.id}/card`]: { pass_id: 74 } });
  const view = renderApp(`/paper/${paper.id}`);
  await screen.findByRole("heading", { name: "SCARCE" });
  return { calls, ...view };
}
const rung = (id: string) => document.getElementById(`dr-${id}`)!;

describe("a paper's Deep read", () => {
  it("is drawn for a card written as a read, each section a rung in the read's order, closed by what it was written from", async () => {
    const { calls } = await open();
    const sections = [...document.querySelectorAll(".dread > .rung.dr-sec")];
    expect(sections.map((s) => s.querySelector(".rl")!.textContent)).toEqual(scarce.sections.map((s) => s.title));
    expect(sections.map((s) => s.getAttribute("data-k"))).toEqual(scarce.sections.map((s) => s.kind));
    // a lens takes its topic's colour
    expect(rung("lens-agents").style.getPropertyValue("--tc")).toBe("#ca8a04");
    expect(within(rung("glance")).getByText(/recent frontier models build their own persistent rules/)).toHaveClass("dr-claim");
    // the old card's rungs are not there
    expect(screen.queryByText("Deep card")).toBeNull();
    expect(screen.queryByText("Summary")).toBeNull();
    // Related work: the prior work as links, then the collection's papers from the API's rows
    const related = within(rung("related"));
    expect(related.getByRole("link", { name: /ProgramBench.*2605\.03546/ })).toHaveAttribute("href", "/paper/2605.03546");
    expect(related.getByText(/Institutions, Institutional Change/).closest("a")).toBeNull();
    expect(related.getByRole("link", { name: /The Helpful Agent Problem/ })).toHaveAttribute("href", "/paper/2610.31985");
    // the foot and Write again are the card's
    expect(screen.getByText(/Written from the LaTeX source of arXiv v2 \(text revision 1\), .*, by gpt-6\.1-sol/)).toBeInTheDocument();
    await userEvent.click(within(document.querySelector(".dread") as HTMLElement).getByRole("button", { name: "Write again" }));
    await waitFor(() => expect(calls.some((c) => c.method === "POST" && c.url === `/api/papers/${paper.id}/card`)).toBe(true));
  });

  it("leaves a Deep card from before as it was: no read, no contents, no chips", async () => {
    await open({ ...paper, card: oldCard });
    expect(document.querySelector(".dread, .dr-toc, .dr-chips")).toBeNull();
    expect(screen.getByText("Deep card").closest(".rung")).toHaveTextContent("A meta-verifier catches 71% of unfaithful chains.");
    expect(screen.getByText("Summary").closest(".rung")).toHaveTextContent("SIFT trains a probe.");
    expect(screen.getByText("Limitations").closest(".rung")).toHaveTextContent("Two models only.");
    expect(screen.getByText(/Written from the LaTeX source/)).toBeInTheDocument();
  });

  it("is a read only by its format: the same sections without it are not drawn as one", async () => {
    const { format: _, ...unmarked } = scarce;
    await open({ ...paper, card: { card: unmarked, ...written, among } });
    expect(document.querySelector(".dread")).toBeNull();
    expect(screen.getByText("Deep card")).toBeInTheDocument();
  });

  it("lists its sections in the rail: the one in view lit, a click scrolling to it, dimmed while the read is out of view", async () => {
    await open();
    const toc = screen.getByRole("navigation", { name: "Deep read" });
    const rows = within(toc).getAllByRole("button");
    expect(rows.map((r) => r.textContent)).toEqual(scarce.sections.map((s) => s.title));
    expect(rows.map((r) => r.getAttribute("data-k"))).toEqual(scarce.sections.map((s) => s.kind));
    expect(within(toc).getByRole("button", { name: "Agents" }).style.getPropertyValue("--tc")).toBe("#ca8a04");
    // under the quick actions
    expect(toc.closest(".pgrail")!.querySelector(".agrid")!.compareDocumentPosition(toc)).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
    const current = () => within(toc).queryAllByRole("button", { current: "location" }).map((r) => r.textContent);
    expect(current()).toEqual([]);
    expect(toc).toHaveClass("away");
    see(".dread");
    expect(toc).not.toHaveClass("away");
    see("#dr-env");
    expect(current()).toEqual(["The environment"]);
    // where one section ends and the next begins, the later one is the one being read
    see("#dr-taxonomy");
    expect(current()).toEqual(["The taxonomy"]);
    see("#dr-taxonomy", false);
    expect(current()).toEqual(["The environment"]);
    // between sections the last one stays
    see("#dr-env", false);
    expect(current()).toEqual(["The environment"]);
    await userEvent.click(within(toc).getByRole("button", { name: "Claims" }));
    expect(scrolled).toHaveBeenCalledWith("dr-claims");
    expect(current()).toEqual(["Claims"]);
    see(".dread", false);
    expect(toc).toHaveClass("away");
    // the phone's strip is not on the desktop
    expect(document.querySelector(".dr-chips")).toBeNull();
  });

  it("has a strip of section chips on the phone: the current one outlined and scrolled into sight, a tap scrolling to its section", async () => {
    const scrollTo = vi.spyOn(Element.prototype, "scrollTo");
    await open(withRead, false);
    const strip = document.querySelector(".dr-chips") as HTMLElement;
    // the first thing in the read, so it sticks for as long as the read is on screen
    expect(strip.parentElement).toHaveClass("dread");
    expect(strip.nextElementSibling).toBe(rung("glance"));
    const chips = within(strip).getAllByRole("button");
    expect(chips.map((c) => c.textContent)).toEqual(scarce.sections.map((s) => s.title));
    expect(strip.querySelector(".cur")).toBeNull();
    scrollTo.mockClear();
    see("#dr-who");
    expect(within(strip).getByRole("button", { current: "location" })).toHaveTextContent("Finding 1 · Who builds institutions");
    expect(scrollTo.mock.contexts).toContain(strip);
    await userEvent.click(within(strip).getByRole("button", { name: "Limitations" }));
    expect(scrolled).toHaveBeenCalledWith("dr-limits");
    // the rail and its contents are the desktop's
    expect(document.querySelector(".dr-toc")).toBeNull();
  });

  it("shows every claim on one line, its verdict first, holds quiet and the rest filled, the evidence opening on a click", async () => {
    await open();
    const ledger = within(rung("claims"));
    const rows = ledger.getAllByRole("button");
    expect(rows).toHaveLength(8);
    expect(rows.map((r) => r.querySelector(".chip")!.textContent!.replace(/^\S+\s/, ""))).toEqual(["holds", "partly", "not shown", "holds", "partly", "holds", "holds", "partly"]);
    for (const row of rows) expect(row.querySelector(".chip")!.classList.contains("solid")).toBe(!row.textContent!.includes("holds"));
    const claim = ledger.getByRole("button", { name: /These arrangements carry across tasks/ });
    expect(claim).toHaveAttribute("aria-expanded", "false");
    expect(ledger.queryByText(/before memory across subruns exists/)).toBeNull();
    await userEvent.click(claim);
    expect(claim).toHaveAttribute("aria-expanded", "true");
    expect(ledger.getByText(/before memory across subruns exists/)).toBeInTheDocument();
    expect(ledger.getByText("· Abstract; §5.3, Tables 1–2")).toBeInTheDocument();
    // one open claim opens no other
    expect(ledger.queryByText(/no measure ties institutional behaviour/)).toBeNull();
    await userEvent.click(claim);
    expect(ledger.queryByText(/before memory across subruns exists/)).toBeNull();
  });

  it("shows a table cell's note on hover and on tap, lights the column under the pointer, and colours a heat table by value", async () => {
    await open();
    const table = within(rung("who")).getByRole("table");
    const cells = (row: string) => [...within(table).getByRole("cell", { name: row }).closest("tr")!.querySelectorAll("td")];
    const sum = within(table).getByRole("cell", { name: "Σ relational" });
    expect(screen.queryByRole("tooltip")).toBeNull();
    await userEvent.hover(sum);
    expect(screen.getByRole("tooltip")).toHaveTextContent("A sum of row percentages");
    // a cell without a note shows none
    await userEvent.hover(cells("Reciprocity")[1]!);
    expect(screen.queryByRole("tooltip")).toBeNull();
    // the column under the pointer, header included; the first column names the rows and is not one
    const lit = () => [...table.querySelectorAll(".hl")];
    expect(lit().length).toBe(15);
    expect(lit().every((c) => (c as HTMLTableCellElement).cellIndex === 1)).toBe(true);
    expect(lit()[0]).toHaveTextContent("Opus-5");
    await userEvent.hover(cells("Reciprocity")[0]!);
    expect(lit()).toEqual([]);
    await userEvent.unhover(table);
    // a tap: the note opens on the cell and closes on a tap elsewhere
    fireEvent.click(sum);
    expect(screen.getByRole("tooltip")).toHaveTextContent("A sum of row percentages");
    fireEvent.click(document.body);
    expect(screen.queryByRole("tooltip")).toBeNull();
    // heat: by value against the table's maximum (50), an empty cell and the row's name left alone
    const heat = (cell: HTMLElement) => cell.style.getPropertyValue("--heat");
    const reputation = cells("Reputation");
    expect(reputation.map(heat)).toEqual(["", "30%", "10%", "6%", "1%", "", "", "", ""]);
    expect(heat(cells("Shortest job first")[4]!)).toBe("50%");
    expect(reputation[5]).toHaveTextContent("·");
    // a table without heat colours nothing
    expect(rung("memory").querySelector("[style*='--heat']")).toBeNull();
  });

  it("lights a diagram's node with its neighbours and dims the rest, its explanation under the drawing", async () => {
    await open();
    const diagram = rung("env").querySelector(".dr-dg") as HTMLElement;
    const d = within(diagram);
    const node = (name: string) => d.getByRole("button", { name });
    expect(d.getByText("Click a box or an arrow for what it does.")).toBeInTheDocument();
    // drawn where the read says: the stored coordinates, not a layout
    expect(node("Scheduler").querySelector("rect")).toHaveAttribute("x", "655");
    expect(node("Scheduler").querySelector("rect")).toHaveAttribute("y", "170");
    await userEvent.click(node("Scheduler"));
    expect(node("Scheduler")).toHaveClass("on");
    expect(node("Scheduler")).toHaveAttribute("aria-pressed", "true");
    const dimmed = () => d.getAllByRole("button").filter((b) => b.classList.contains("dim")).map((b) => b.getAttribute("aria-label"));
    const all = d.getAllByRole("button").map((b) => b.getAttribute("aria-label"));
    // its two neighbours and the two edges to them stay lit
    expect(all.filter((n) => !dimmed().includes(n))).toEqual(["4 Ballot → Scheduler", "Scheduler → Execution window", "4 Ballot", "Scheduler", "Execution window"]);
    const tip = diagram.querySelector(".tipbox")!;
    expect(tip).toHaveTextContent("Scheduler");
    expect(tip).toHaveTextContent("Grants windows in the agreed order.");
    expect(tip).toHaveTextContent("§3.1");
    // an edge lights itself and its two ends, by its own title where it has one
    await userEvent.click(node("Subrun end"));
    expect(all.filter((n) => !dimmed().includes(n))).toEqual(["Subrun end", "Execution window", "Evaluator"]);
    expect(tip).toHaveTextContent("Rounds repeat until the deadline");
    // inline text in an explanation
    await userEvent.click(node("Worker ×5"));
    expect(within(tip as HTMLElement).getByText("request_execution_slot").tagName).toBe("CODE");
    // the same one again puts it back
    await userEvent.click(node("Worker ×5"));
    expect(dimmed()).toEqual([]);
    expect(d.getByText("Click a box or an arrow for what it does.")).toBeInTheDocument();
  });

  it("shows a figure from the paper's stored figures, large on a click, and its caption over a placeholder when the image fails", async () => {
    await open();
    const img = within(rung("env")).getByRole("img", { name: "Figure 2" });
    expect(img).toHaveAttribute("src", `/api/papers/${paper.id}/deep/figures/methodology_by_step?v=${encodeURIComponent(written.written_at)}`);
    await userEvent.click(within(rung("env")).getByRole("button", { name: "Figure 2, larger" }));
    const zoom = screen.getByRole("dialog", { name: "Figure 2" });
    expect(within(zoom).getByRole("img")).toHaveAttribute("src", img.getAttribute("src"));
    expect(within(zoom).getByRole("button", { name: "Close the figure" })).toHaveFocus();
    await userEvent.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.getByRole("button", { name: "Figure 2, larger" })).toHaveFocus();
    fireEvent.error(img);
    expect(within(rung("env")).queryByRole("img")).toBeNull();
    expect(within(rung("env")).getByText("Figure 2 · the image did not load")).toBeInTheDocument();
    expect(within(rung("env")).getByText(/The round: queue with bids, private messages, ballots, scheduler\./)).toBeInTheDocument();
    // the other figures are untouched
    expect(within(rung("who")).getByRole("img", { name: "Figure 6" })).toBeInTheDocument();
  });

  it("keeps its process notes folded until opened, after the last section", async () => {
    await open();
    const fold = screen.getByRole("button", { name: "Process notes · 1" });
    expect(fold).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByText(/history could not be listed/)).toBeNull();
    expect(rung("repro").compareDocumentPosition(fold)).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
    await userEvent.click(fold);
    const notes = fold.closest(".rung") as HTMLElement;
    expect(within(notes).getByText("access")).toHaveClass("tag");
    expect(within(notes).getByText(/history could not be listed/)).toBeInTheDocument();
    expect(within(notes).getByRole("link", { name: /github\.com/ })).toHaveAttribute("href", "https://github.com/compass-group-tue/SCARCE");
  });

  it("draws every block type, each where a screenshot can find it", async () => {
    await open({ ...paper, card: { card: icl, ...written, among: [] } });
    const block = (section: string, type: string) => {
      const blocks = (icl.sections.find((s) => s.id === section) as unknown as { blocks: { type: string }[] }).blocks;
      return document.querySelector(`[data-block="${section}.${blocks.findIndex((b) => b.type === type)}"]`) as HTMLElement;
    };
    expect(block("problem", "p")).toHaveTextContent("Show a base model a dozen lines like The soup was cold -> bar");
    expect(block("problem", "p").querySelector("code")).toHaveTextContent("The soup was cold -> bar");
    expect(block("problem", "why").querySelector(".dr-why")).toHaveTextContent(/^Arbitrary labels/);
    expect(within(block("problem", "h3")).getByRole("heading", { name: "Setup" })).toBeInTheDocument();
    expect(within(block("problem", "list")).getAllByRole("listitem")).toHaveLength(3);
    expect(block("read", "eq").querySelector(".katex-display")).not.toBeNull();
    expect(block("read", "eq")).toHaveTextContent("Proposition 1.");
    expect(block("read", "p").querySelectorAll(".dr-tex").length).toBe(5);
    expect(within(block("read", "table")).getByRole("columnheader", { name: "Llama-3.2-3B" })).toBeInTheDocument();
    expect(block("gate", "note").querySelector(".dr-note")).toHaveTextContent("Llama's gate is much weaker");
    expect(block("gate", "note")).toHaveTextContent("· §5, Table 3");
    const xp = block("slot", "xp");
    expect(xp).toHaveTextContent("1aWhere does the label's identity live?");
    expect([...xp.querySelectorAll(".x > span")].map((s) => s.textContent)).toEqual(["Did", "Found", "Means", "Not shown"]);
    expect(within(block("circuit", "diagram")).getByRole("button", { name: "Reader heads" })).toBeInTheDocument();
    expect(within(block("circuit", "fig")).getByRole("img", { name: "Figure 2" })).toBeInTheDocument();
    // the glance's facts: text, and what is released as marks
    const code = within(rung("glance")).getByText("Code").closest(".f") as HTMLElement;
    expect([...code.querySelectorAll(".dr-tag.yes")].map((t) => t.textContent)).toEqual(["code ✓", "batteries ✓", "toy model ✓"]);
    // limitations say whose they are
    expect([...rung("limits").querySelectorAll(".who")].map((w) => w.textContent)).toEqual(["paper", "paper", "paper", "paper", "read", "read", "read"]);
  });
});
