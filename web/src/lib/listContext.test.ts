import { describe, expect, it } from "vitest";
import { forgetList, openedFrom, placeOf } from "./listContext";

describe("placeOf", () => {
  it("counts against the whole list, not the rows loaded so far", () => {
    openedFrom("papers:rare", ["a", "b", "c"], 40);
    expect(placeOf("b", "papers:rare")).toEqual({ index: 2, total: 40, prev: "a", next: "c" });
    expect(placeOf("z", "papers:rare")).toBeNull();
    forgetList();
    expect(placeOf("a", "papers:rare")).toBeNull();
  });

  it("answers only for the list the page was opened from, not a list opened since", () => {
    openedFrom("papers:rare", ["a", "b"], 2);
    openedFrom("topic:mi:relevant", ["b", "c"], 2);
    expect(placeOf("b", "topic:mi:relevant")?.index).toBe(1);
    expect(placeOf("b", "papers:rare")).toBeNull();
    expect(placeOf("b", undefined)).toBeNull();
  });
});
