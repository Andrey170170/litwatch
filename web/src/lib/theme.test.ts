import { afterEach, describe, expect, it, vi } from "vitest";
import { applyAppearance, KEY, readAppearance, readStored, setAppearance } from "./theme";

describe("the appearance", () => {
  afterEach(() => setAppearance("system"));

  it("reads System for nothing kept or anything but a side", () => {
    expect(readStored()).toBe("system");
    localStorage.setItem(KEY, "sepia");
    expect(readStored()).toBe("system");
    localStorage.setItem(KEY, "light");
    expect(readStored()).toBe("light");
  });

  it("holds the choice for the page when the device keeps nothing", () => {
    const setItem = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("quota", "QuotaExceededError");
    });
    setAppearance("dark");
    expect(readAppearance()).toBe("dark");
    expect(document.documentElement.dataset.theme).toBe("dark");
    setItem.mockRestore();
  });

  it("forces a side on the document and the PWA's bar colour, and lets both follow the OS again", () => {
    document.head.innerHTML =
      '<meta name="theme-color" content="#e9ebe7" media="(prefers-color-scheme: light)" /><meta name="theme-color" content="#07090d" media="(prefers-color-scheme: dark)" />';
    const bars = () => [...document.querySelectorAll<HTMLMetaElement>('meta[name="theme-color"]')].map((m) => m.content);
    applyAppearance("dark");
    expect(document.documentElement.dataset.theme).toBe("dark");
    expect(bars()).toEqual(["#07090d", "#07090d"]);
    applyAppearance("system");
    expect(document.documentElement.dataset.theme).toBeUndefined();
    expect(bars()).toEqual(["#e9ebe7", "#07090d"]);
  });
});
