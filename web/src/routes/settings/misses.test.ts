import { describe, expect, it } from "vitest";
import type { Schemas } from "@/api/client";
import { cells, missedAt } from "./misses";

type Trace = Schemas["Miss"]["trace"];
const admitted: Trace = { classified: true, probability: 0.3, threshold: 0.5, classifier: false, below: false, uncertain: false, keyword: true, graded: true, fit: null, graders: [] };
const graded = (t: Trace) => cells(t).slice(3).map((c) => [c.name, c.value, c.tone]);

describe("a miss's trace", () => {
  it("reads a verdict by the graders its depth asked and the Fit that stands, not as a Focus pair", () => {
    const field: Trace = { ...admitted, fit: "central", graders: [{ role: "field_grade", fit: "central" }] };
    expect(graded(field)).toEqual([["Field grade", "central", "ok"]]);
    expect(missedAt(field)).toBe("nowhere: the trace admits it");
    // Area: the judge said central, its check doubted it and the check let it go; none stands.
    const area: Trace = { ...admitted, fit: "none", graders: [{ role: "judge", fit: "central" }, { role: "area_check", holds: false }, { role: "check", fit: "none" }] };
    expect(graded(area)).toEqual([["Judge", "central", "ok"], ["Area check", "doubts", "off"], ["Check", "none", "bad"]]);
    expect(missedAt(area)).toBe("grading");
  });
  it("shows grading as one pending cell until a verdict exists", () => {
    expect(graded({ ...admitted, graded: false })).toEqual([["Grading", "pending", "off"]]);
    expect(missedAt({ ...admitted, graded: false })).toBe("not graded yet");
  });
});
