import { describe, expect, it } from "vitest";
import { PAPER_SHELVES, segmentFlex, shelvesFor, TOPIC_SHELVES } from "./rarity";

describe("shelves", () => {
  it("cuts Papers by rarity, the top two open as reading rows, the lower ones folded", () => {
    expect(PAPER_SHELVES.map((s) => [s.key, s.form, s.open])).toEqual([
      ["top", "reading", true],
      ["rare", "table", true],
      ["uncommon", "table", false],
      ["common", "table", false],
    ]);
  });
  it("keeps only the shelves a rarity filter leaves something on", () => {
    expect(shelvesFor(PAPER_SHELVES, ["rare", "common"]).map((s) => s.key)).toEqual(["rare", "common"]);
    expect(shelvesFor(PAPER_SHELVES, []).length).toBe(PAPER_SHELVES.length);
  });
  it("cuts a topic page by grade with must reads first", () => {
    expect(TOPIC_SHELVES[0]).toMatchObject({ grade: ["must_read"], form: "reading", open: true });
  });
  it("sizes rarity segments by count with a floor so empty levels stay tappable", () => {
    const flex = segmentFlex({ legendary: 0, epic: 3, rare: 20, uncommon: 50, common: 100 });
    expect(flex.legendary).toBeCloseTo(0.07);
    expect(flex.common).toBeGreaterThan(flex.rare);
  });
});
