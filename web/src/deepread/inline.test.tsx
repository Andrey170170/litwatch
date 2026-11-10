import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { Inline } from "./inline";

const draw = (text: string) => render(<Inline text={text} />).container;

describe("a read's inline text", () => {
  it("marks bold, italic, code, maths and links, nested where they nest", () => {
    const el = draw("The **eight *sharpest* readers** use `W_K` at $n_m$, see [the code](https://example.org/x).");
    expect(el.querySelector("b")).toHaveTextContent("eight sharpest readers");
    expect(el.querySelector("b i")).toHaveTextContent("sharpest");
    expect(el.querySelector("code")).toHaveTextContent("W_K");
    expect(el.querySelector(".dr-tex .katex")).not.toBeNull();
    const link = el.querySelector("a")!;
    expect(link).toHaveAttribute("href", "https://example.org/x");
    expect(link).toHaveAttribute("rel", "noopener noreferrer");
    expect(el).toHaveTextContent(/^The eight sharpest readers use W_K at .* see the code\.$/);
  });

  it("shows HTML and any other markup as written, and links only an http(s) address", () => {
    const el = draw('<img src=x onerror=alert(1)> <b>not bold</b> [run](javascript:alert(1)) [file](file:///etc/passwd)');
    expect(el.querySelector("img, b, a")).toBeNull();
    expect(el).toHaveTextContent('<img src=x onerror=alert(1)> <b>not bold</b> [run](javascript:alert(1)) [file](file:///etc/passwd)');
  });

  it("leaves amounts of money, lone marks and escaped marks as text", () => {
    expect(draw("It costs $5 to $10 a run, 2 * 3 * 4 and a_b.")).toHaveTextContent("It costs $5 to $10 a run, 2 * 3 * 4 and a_b.");
    expect(draw("a $ sign, a * star").querySelector(".dr-tex, i")).toBeNull();
    const el = draw("\\*not italic\\* and \\$x\\$ and \\`code\\`");
    expect(el.querySelector("i, code, .dr-tex")).toBeNull();
    expect(el).toHaveTextContent("*not italic* and $x$ and `code`");
  });

  it("keeps code literal: no maths or bold inside it", () => {
    const el = draw("`**a** $b$`");
    expect(el.querySelector("code")).toHaveTextContent("**a** $b$");
    expect(el.querySelector("b, .dr-tex")).toBeNull();
  });
});
