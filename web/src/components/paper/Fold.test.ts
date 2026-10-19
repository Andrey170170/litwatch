import { renderHook } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { arrange, byGrade, foldCount, folded, useSettledTopics } from "./Fold";
import type { PaperTopic } from "./TopicRow";

const t = (slug: string, grade: PaperTopic["grade"], extra: Partial<PaperTopic> = {}): PaperTopic => ({ topic: { slug, name: slug, depth: "focus", monogram: slug, colour: "#fff" }, grade, rarity: grade === "must_read" ? "epic" : grade === "adjacent" ? "uncommon" : "rare", missed: false, ...extra });
const slugs = (ts: PaperTopic[]) => ts.map((x) => x.topic.slug);

describe("the paper's topic rows", () => {
  it("go must read, relevant, adjacent, none, then ungraded, ties in the API's order", () => {
    const topics = [t("a", "none"), t("b", "adjacent"), t("c", null), t("d", "must_read"), t("e", "adjacent"), t("f", "relevant"), t("g", "must_read", { state: "ungraded" })];
    expect(slugs(byGrade(topics))).toEqual(["d", "f", "b", "e", "a", "c", "g"]);
  });

  it("keep the order they opened in when a grade changes, and add a new topic after it", () => {
    const order = ["b", "a"];
    expect(slugs(arrange([t("a", "must_read"), t("b", "none"), t("c", "adjacent"), t("d", "relevant")], order))).toEqual(["b", "a", "d", "c"]);
  });

  it("fold only after two rows", () => {
    expect(folded([t("a", "none"), t("b", "none")])).toEqual([]);
    expect(slugs(folded([t("a", "none"), t("b", "none"), t("c", "none")]))).toEqual(["c"]);
  });

  it("count the hidden rows by grade, in grade order and their rows' colours", () => {
    const hidden = [t("a", "adjacent"), t("b", "must_read"), t("c", "adjacent"), t("d", "adjacent"), t("e", "relevant", { state: "grading_again" }), t("f", null)];
    expect(foldCount(hidden)).toEqual({
      total: 6,
      grades: [
        { rank: "must_read", name: "must read", n: 1, colour: "var(--r-epic)" },
        { rank: "adjacent", name: "adjacent", n: 3, colour: "var(--r-uncommon)" },
        // the first ungraded row is grey, grading again; a plain ungraded one keeps its rarity, as its row does
        { rank: "ungraded", name: "ungraded", n: 2, colour: "var(--fg3)" },
      ],
    });
    expect(foldCount([t("f", null)]).grades[0]!.colour).toBe("var(--r-rare)");
  });
});

describe("the order a page settles on", () => {
  it("holds through a regrade, and a topic that arrives later keeps the place it first took", () => {
    const { result, rerender } = renderHook(({ topics }) => useSettledTopics(topics), { initialProps: { topics: [] as PaperTopic[] } });
    expect(result.current).toEqual([]);
    // classified by hand: two topics arrive, then a label makes the second must read
    rerender({ topics: [t("a", "adjacent"), t("b", "none")] });
    expect(slugs(result.current)).toEqual(["a", "b"]);
    rerender({ topics: [t("a", "adjacent"), t("b", "must_read")] });
    expect(slugs(result.current)).toEqual(["a", "b"]);
  });
});
