import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { Prose } from "@/components/Prose";

describe("Prose", () => {
  it("splits paragraphs at blank lines and leaves a Brief's own links as written", () => {
    render(<Prose text={"Code is [available](https://github.com/x/y).\nStill one paragraph.\n\nA second."} />);
    expect(screen.getByText(/^Code is \[available\]\(https:\/\/github.com\/x\/y\)\. Still one paragraph\.$/)).toBeInTheDocument();
    expect(screen.getByText("A second.")).toBeInTheDocument();
    expect(screen.queryByRole("link")).toBeNull();
  });

  it("takes only an arXiv id as a digest's mark", () => {
    render(<Prose marks text="See [the repo](https://github.com/x/y)." />);
    expect(screen.getByText("See [the repo](https://github.com/x/y).")).toBeInTheDocument();
    expect(screen.queryByRole("link")).toBeNull();
  });
});
