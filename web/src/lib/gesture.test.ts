import { afterEach, describe, expect, it, vi } from "vitest";
import { axisOf, backCommits, drawerSettles, motionMs, resist, sheetCloses, stepCommits, Tracker } from "./gesture";

describe("A drag's axis", () => {
  it("is undecided within the slop, vertical when as tall as wide, horizontal otherwise", () => {
    expect(axisOf(5, 5)).toBeNull();
    expect(axisOf(7, 0)).toBeNull();
    expect(axisOf(0, 9)).toBe("y");
    expect(axisOf(9, 9)).toBe("y");
    expect(axisOf(-12, 6)).toBe("x");
  });
});

describe("The release velocity", () => {
  it("is read over the last 90 ms, so a slow drag that ends in a flick counts as the flick", () => {
    const t = new Tracker();
    t.push({ t: 0, x: 0, y: 0 });
    t.push({ t: 400, x: 20, y: 0 });
    t.push({ t: 800, x: 40, y: 0 });
    expect(t.velocity().vx).toBeCloseTo(0.05);
    t.push({ t: 850, x: 90, y: 0 });
    t.push({ t: 880, x: 150, y: 0 });
    expect(t.velocity().vx).toBeCloseTo(110 / 80);
  });
  it("is zero with one sample or no time between them", () => {
    const t = new Tracker();
    t.push({ t: 10, x: 0, y: 0 });
    expect(t.velocity()).toEqual({ vx: 0, vy: 0 });
    t.push({ t: 10, x: 50, y: 0 });
    expect(t.velocity()).toEqual({ vx: 0, vy: 0 });
  });
});

describe("Where a step settles", () => {
  const w = 390;
  it("commits past a quarter of the width, towards the next when dragged left", () => {
    expect(stepCommits(-98, 0, w)).toBe(1);
    expect(stepCommits(98, 0, w)).toBe(-1);
    expect(stepCommits(-97, 0, w)).toBe(0);
  });
  it("commits on a flick the way it was dragged, whatever the distance", () => {
    expect(stepCommits(-20, -0.6, w)).toBe(1);
    expect(stepCommits(30, 0.5, w)).toBe(-1);
    // a flick back towards the start is a change of mind
    expect(stepCommits(-120, 0.8, w)).toBe(0);
    expect(stepCommits(-20, 0.6, w)).toBe(0);
  });
  it("resists where nothing lies: the content follows a quarter of the finger", () => {
    expect(resist(100)).toBe(25);
    expect(resist(-40)).toBe(-10);
  });
});

describe("Where the drawer settles", () => {
  const w = 300;
  it("by position when the finger let go slowly: open past half the width", () => {
    expect(drawerSettles(-140, 0, w)).toBe("open");
    expect(drawerSettles(-160, 0.1, w)).toBe("shut");
  });
  it("by direction when flicked", () => {
    expect(drawerSettles(-250, 0.7, w)).toBe("open");
    expect(drawerSettles(-20, -0.7, w)).toBe("shut");
  });
});

describe("A page dragged back and a sheet dragged down", () => {
  it("go past a third, or on a flick that way", () => {
    expect(backCommits(130, 0, 390)).toBe(true);
    expect(backCommits(129, 0.2, 390)).toBe(false);
    expect(backCommits(30, 0.6, 390)).toBe(true);
    expect(sheetCloses(100, 0, 300)).toBe(true);
    expect(sheetCloses(99, 0, 300)).toBe(false);
    expect(sheetCloses(10, 0.5, 300)).toBe(true);
    expect(sheetCloses(10, -0.9, 300)).toBe(false);
  });
});

describe("Motion under reduced motion", () => {
  afterEach(() => vi.unstubAllGlobals());
  it("takes no time", () => {
    vi.stubGlobal("matchMedia", (q: string) => ({ matches: q.includes("reduced-motion") }));
    expect(motionMs(200)).toBe(0);
    vi.stubGlobal("matchMedia", () => ({ matches: false }));
    expect(motionMs(200)).toBe(200);
  });
});
