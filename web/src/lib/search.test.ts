import { describe, expect, it } from "vitest";
import { listSearch, parseSearch, sortOf, stringifySearch, toggled } from "./search";

describe("list search params", () => {
  it("drops unknown and malformed values", () => {
    expect(listSearch({ q: "", rarity: "epic,bogus,rare", saved: "1", code: "no", sort: "oldest", topic: "" })).toEqual({ rarity: ["epic", "rare"], saved: true });
  });
  it("round-trips through a plain query string", () => {
    const s = { q: "probing", rarity: ["epic", "rare"], saved: true, topic: ["mech-interp"] };
    const qs = stringifySearch(s);
    expect(qs).toBe("?q=probing&rarity=epic%2Crare&saved=1&topic=mech-interp");
    expect(listSearch(parseSearch(qs))).toEqual(s);
  });
  it("sorts by best match while a search is typed, rarest otherwise", () => {
    expect(sortOf({})).toBe("rarest");
    expect(sortOf({ q: "x" })).toBe("best");
    expect(sortOf({ q: "x", sort: "newest" })).toBe("newest");
    expect(sortOf({ sort: "best" })).toBe("rarest");
  });
  it("toggles a value and clears the key when the list empties", () => {
    expect(toggled(undefined, "epic")).toEqual(["epic"]);
    expect(toggled(["epic"], "epic")).toBeUndefined();
  });
});
