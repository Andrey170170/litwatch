import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Thumbs } from "./Thumbs";
import { deferred, json, renderWithClient, scriptFetch } from "@/test/harness";

afterEach(() => vi.unstubAllGlobals());

describe("Thumbs writes", () => {
  it("go out one at a time, in tap order, so the reason lands last", async () => {
    const first = deferred<Response>();
    let puts = 0;
    const calls = scriptFetch({
      "PUT /api/reactions/2505.17244": () => (++puts === 1 ? first.promise : json({ message: "Marked not relevant for T", saved: false })),
    });
    renderWithClient(<Thumbs paperId="2505.17244" topic="t" value={null} reason={null} />);
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: /thumb down/i }));
    await user.click(screen.getByRole("button", { name: "Unrelated to the topic" }));
    // The reasonless PUT is still out; the second waits behind it.
    expect(calls.filter((c) => c.method === "PUT")).toHaveLength(1);
    first.resolve(json({ message: "Remembered for T", saved: false }));
    await waitFor(() => expect(calls.filter((c) => c.method === "PUT")).toHaveLength(2));
    expect(calls.filter((c) => c.method === "PUT").map((c) => (c.body as { reason: string | null }).reason)).toEqual([null, "unrelated"]);
    expect(screen.getByRole("button", { name: /thumb down/i })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByText("Unrelated to the topic")).toBeInTheDocument();
  });

  it("a cleared thumb's DELETE waits for the PUT before it", async () => {
    const put = deferred<Response>();
    const calls = scriptFetch({
      "PUT /api/reactions/p": () => put.promise,
      "DELETE /api/reactions/p": () => json({ message: "Taken back", saved: false }),
    });
    renderWithClient(<Thumbs paperId="p" topic={null} value={null} reason={null} />);
    const user = userEvent.setup();
    const down = screen.getByRole("button", { name: /thumb down/i });
    await user.click(down);
    await user.click(down);
    expect(calls.map((c) => c.method)).toEqual(["PUT"]);
    put.resolve(json({ message: "Remembered", saved: false }));
    await waitFor(() => expect(calls.map((c) => c.method)).toEqual(["PUT", "DELETE"]));
    expect(down).toHaveAttribute("aria-pressed", "false");
  });

  it("a failed write puts the thumb back where the server has it", async () => {
    scriptFetch({ "PUT /api/reactions/p": () => json({ detail: "boom" }, 500) });
    renderWithClient(<Thumbs paperId="p" topic="t" value={null} reason={null} />);
    const user = userEvent.setup();
    const up = screen.getByRole("button", { name: /thumb up/i });
    await user.click(up);
    await waitFor(() => expect(up).toHaveAttribute("aria-pressed", "false"));
  });

  it("an older failure does not undo a newer intent", async () => {
    const first = deferred<Response>();
    let puts = 0;
    scriptFetch({ "PUT /api/reactions/p": () => (++puts === 1 ? first.promise : json({ message: "Remembered for T", saved: false })) });
    renderWithClient(<Thumbs paperId="p" topic="t" value={null} reason={null} />);
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: /thumb down/i }));
    await user.click(screen.getByRole("button", { name: "Already knew it" }));
    first.resolve(json({ detail: "boom" }, 500));
    await waitFor(() => expect(puts).toBe(2));
    await waitFor(() => expect(screen.getByRole("button", { name: /thumb down/i })).toHaveAttribute("aria-pressed", "true"));
    expect(screen.getByText("Already knew it")).toBeInTheDocument();
  });
});
