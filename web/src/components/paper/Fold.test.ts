import { describe, expect, it } from "vitest";
import { arrange, byGrade, foldCount, folded } from "./Fold";
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
    const hidden = [t("a", "adjacent"), t("b", "must_read"), t("c", "adjacent"), t("d", "adjacent"), t("e", "relevant", { state: "grading_again" })];
    expect(foldCount(hidden)).toEqual({
      total: 5,
      grades: [
        { rank: "must_read", name: "must read", n: 1, colour: "var(--r-epic)" },
        { rank: "adjacent", name: "adjacent", n: 3, colour: "var(--r-uncommon)" },
        { rank: "ungraded", name: "ungraded", n: 1, colour: "var(--fg3)" },
      ],
    });
  });
});
