import { describe, expect, it } from "vitest";
import { authorsLine } from "./format";

describe("authorsLine", () => {
  it("lists surnames and folds the tail into 'and N more'", () => {
    expect(authorsLine(["Ada Lovelace", "Alan Turing"])).toBe("Lovelace, Turing");
    expect(authorsLine(["A One", "B Two", "C Three", "D Four"])).toBe("One, Two, Three, Four");
    expect(authorsLine(["A One", "B Two", "C Three", "D Four", "E Five", "F Six"])).toBe("One, Two, Three and 3 more");
  });
});
