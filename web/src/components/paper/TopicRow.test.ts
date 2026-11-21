import { describe, expect, it } from "vitest";
import { type PaperTopic, whoseFit } from "./TopicRow";

const t = (extra: Partial<PaperTopic>): PaperTopic => ({ topic: { slug: "cf", name: "CoT", depth: "focus", monogram: "CF", colour: "#fff" }, fit: "related", missed: false, ...extra });

describe("whose Fit a topic row shows", () => {
  it("names what the API says set the owner's word, whatever else the row holds", () => {
    // A miss's label matches the word, and is still the miss's.
    expect(whoseFit(t({ word: "central", word_from: "miss", missed: true, label: { value: "central", origin: "miss" } }))).toBe("reported as missed: central");
    // A newer thumb outranks a label that is still there.
    expect(whoseFit(t({ word: "none", word_from: "reaction", label: { value: "none", origin: "reader" }, reaction: { value: "down", reason: "wrong_topic" } }))).toBe("your mark: none");
    expect(whoseFit(t({ word: "central", word_from: "label", label: { value: "central", origin: "sample" } }))).toBe("your label: central");
  });

  it("is the pipeline's without a word", () => {
    expect(whoseFit(t({}))).toBe("pipeline: related");
    expect(whoseFit(t({ fit: null }))).toBeNull();
  });
});
