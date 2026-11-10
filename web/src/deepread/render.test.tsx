import { act, within } from "@testing-library/react";
import { beforeAll, describe, expect, it, vi } from "vitest";
import { viewport } from "@/test/harness";
import icl from "./fixtures/read-icl.json";
import scarce from "./fixtures/read-scarce.json";

const PNG = "data:image/png;base64,iVBORw0KGgo=";
const fetched = vi.fn();
const html = document.documentElement;
const draw = (read: unknown, options?: Parameters<typeof window.renderRead>[1]) => act(() => window.renderRead(read, options));

beforeAll(async () => {
  vi.stubGlobal("fetch", fetched);
  viewport(true);
  document.body.innerHTML = '<div id="root"></div>';
  await import("./render");
});

describe("the render page", () => {
  it("draws the read it is handed, from what it is handed alone, and says when it is ready", async () => {
    expect(html.dataset.render).toBeUndefined();
    await draw(scarce, { figures: { methodology_by_step: PNG }, topics: { agents: "#ca8a04" }, theme: "dark" });
    expect(html.dataset.render).toBe("ready");
    expect(html.dataset.theme).toBe("dark");
    // every section and every block is where a screenshot can find it
    for (const s of scarce.sections) {
      expect(document.querySelector(`[data-section="${s.id}"]`)).toBe(document.getElementById(`dr-${s.id}`));
      const blocks = "blocks" in s ? (s.blocks as unknown[]) : [];
      expect(document.querySelectorAll(`[data-block^="${s.id}."]`)).toHaveLength(blocks.length);
    }
    expect(document.querySelector('[data-block="env.0"] .dr-dg')).not.toBeNull();
    expect(document.getElementById("dr-lens-agents")!.style.getPropertyValue("--tc")).toBe("#ca8a04");
    // a figure handed in is drawn at once; one left out shows its placeholder, and nothing is asked of the API
    const given = within(document.getElementById("dr-env")!).getByRole("img", { name: "Figure 2" });
    expect(given).toHaveAttribute("src", PNG);
    expect(given).toHaveAttribute("loading", "eager");
    expect(document.getElementById("dr-who")!.querySelector("img")).toBeNull();
    expect(document.getElementById("dr-who")).toHaveTextContent("Figure 6 · the image is not here");
    // the collection's papers come from the read itself, and lead nowhere
    const related = document.getElementById("dr-related")!;
    expect(related).toHaveTextContent("The Helpful Agent Problem");
    expect(related.querySelector("a")).toBeNull();
    // no app around it: no shell, no rail, no Write again
    expect(document.querySelector(".side, .pgrail, .dr-toc, .cfoot")).toBeNull();
    expect(fetched).not.toHaveBeenCalled();
  });

  it("draws another draft in the first one's place, at the phone's width with its chips", async () => {
    viewport(false);
    await draw(icl);
    expect(html.dataset.render).toBe("ready");
    // no theme given: the page follows the browser again
    expect(html.dataset.theme).toBeUndefined();
    expect(document.getElementById("dr-env")).toBeNull();
    expect(document.querySelectorAll(".dread")).toHaveLength(1);
    expect([...document.querySelectorAll(".dr-chips button")].map((c) => c.textContent)).toEqual(icl.sections.map((s) => s.title));
    // a lens with no colour given stays neutral
    expect(document.getElementById("dr-lens-circuits")!.style.getPropertyValue("--tc")).toBe("");
    expect(fetched).not.toHaveBeenCalled();
  });

  it("refuses what is not a read, and says so on the page", async () => {
    const { format: _, ...card } = scarce;
    await expect(draw(card)).rejects.toThrow('not a Deep read: its format is not "read"');
    expect(html.dataset.render).toBe("error");
    expect(html.dataset.renderError).toBe('not a Deep read: its format is not "read"');
    // the next draft clears it
    await draw(scarce);
    expect(html.dataset.render).toBe("ready");
    expect(html.dataset.renderError).toBeUndefined();
  });

  it("says a draft it could not draw is an error, not ready", async () => {
    // outside `act`, which would take the failed render for itself: this is how the page meets it in a browser
    const quiet = vi.spyOn(console, "error").mockImplementation(() => {});
    await expect(window.renderRead({ format: "read", sections: [{ id: "m", kind: "method", title: "Method" }] })).rejects.toThrow(TypeError);
    quiet.mockRestore();
    expect(html.dataset.render).toBe("error");
    expect(html.dataset.renderError).not.toBe("");
    expect(document.querySelector(".dread")).toBeNull();
    await draw(icl);
    expect(html.dataset.render).toBe("ready");
    expect(document.querySelectorAll(".dread")).toHaveLength(1);
  });
});
