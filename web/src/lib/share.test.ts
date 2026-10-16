import { describe, expect, it } from "vitest";
import { arxivIdIn, landingFor } from "./share";

describe("An arXiv id in a share", () => {
  it("is read from an abs or pdf link, with its version dropped", () => {
    expect(arxivIdIn("https://arxiv.org/abs/2401.12345v2")).toBe("2401.12345");
    expect(arxivIdIn("https://arxiv.org/pdf/1706.03762.pdf")).toBe("1706.03762");
    expect(arxivIdIn("https://arxiv.org/abs/cs/9901001")).toBe("cs/9901001");
    expect(arxivIdIn("arXiv:math.GT/0309136v1")).toBe("math.GT/0309136");
  });
  it("is the link's, whatever its style, before one the text mentions", () => {
    expect(arxivIdIn("https://arxiv.org/abs/hep-th/9901001", "see also 2401.12345")).toBe("hep-th/9901001");
  });
  it("is read from a text when the link has none, and not from a longer number", () => {
    expect(arxivIdIn("https://x.com/a/status/1", "Great paper 2310.06825 by ...")).toBe("2310.06825");
    expect(arxivIdIn("order 12401.12345 shipped", "1 2401.1234567")).toBeNull();
    expect(arxivIdIn(null, undefined, "")).toBeNull();
  });
});

describe("Where a share lands", () => {
  it("on the paper when an id is found, else on a search for what was shared", () => {
    expect(landingFor({ url: "https://arxiv.org/abs/2401.12345", title: "x" })).toEqual({ kind: "paper", id: "2401.12345" });
    expect(landingFor({ title: " Attention is all you need ", url: "https://example.org/post" })).toEqual({ kind: "search", q: "Attention is all you need" });
    expect(landingFor({})).toEqual({ kind: "search", q: "" });
  });
});
