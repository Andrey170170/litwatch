import { describe, expect, it } from "vitest";
import { forgetList, openedFrom, placeOf } from "./listContext";

describe("placeOf", () => {
  it("counts against the whole list, not the rows loaded so far", () => {
    openedFrom("papers:rare", ["a", "b", "c"], 40);
    expect(placeOf("b")).toEqual({ index: 2, total: 40, prev: "a", next: "c" });
    expect(placeOf("z")).toBeNull();
    forgetList();
    expect(placeOf("a")).toBeNull();
  });
});
