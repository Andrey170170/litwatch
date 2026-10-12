import { describe, expect, it } from "vitest";
import { initial, reduce, type ThumbAction, type ThumbState } from "./thumbs";

function run(start: ThumbState, actions: ThumbAction[]) {
  const effects = [];
  let s = start;
  for (const a of actions) {
    const [next, effect] = reduce(s, a);
    s = next;
    effects.push(effect);
  }
  return { state: s, effects };
}

describe("thumb up", () => {
  it("tap 1 sets it, tap 2 offers Great find, tap 3 clears it", () => {
    const { state, effects } = run(initial(null, null), [{ type: "tapUp" }, { type: "tapUp" }, { type: "tapUp" }]);
    expect(effects).toEqual([{ kind: "put", value: "up", reason: null }, { kind: "none" }, { kind: "delete" }]);
    expect(state).toEqual({ value: null, reason: null, open: null });
  });

  it("the second tap opens the Great find chip", () => {
    const { state } = run(initial(null, null), [{ type: "tapUp" }, { type: "tapUp" }]);
    expect(state.open).toBe("great");
  });

  it("picking Great find records it and closes the chip", () => {
    const { state, effects } = run(initial("up", null), [{ type: "tapUp" }, { type: "pick", reason: "great_find" }]);
    expect(effects[1]).toEqual({ kind: "put", value: "up", reason: "great_find" });
    expect(state).toEqual({ value: "up", reason: "great_find", open: null });
  });

  it("an up carrying Great find clears on the next tap", () => {
    const { effects } = run(initial("up", "great_find"), [{ type: "tapUp" }]);
    expect(effects[0]).toEqual({ kind: "delete" });
  });

  it("switches from down to up in one tap", () => {
    const { state, effects } = run(initial("down", "overrated"), [{ type: "tapUp" }]);
    expect(effects[0]).toEqual({ kind: "put", value: "up", reason: null });
    expect(state.value).toBe("up");
  });
});

describe("thumb down", () => {
  it("tap 1 sets it and opens the reasons, tap 2 clears it", () => {
    const first = run(initial(null, null), [{ type: "tapDown" }]);
    expect(first.effects[0]).toEqual({ kind: "put", value: "down", reason: null });
    expect(first.state.open).toBe("reasons");
    const second = run(first.state, [{ type: "tapDown" }]);
    expect(second.effects[0]).toEqual({ kind: "delete" });
    expect(second.state).toEqual({ value: null, reason: null, open: null });
  });

  it("the first tap on a reason records it and closes the chips", () => {
    const { state, effects } = run(initial(null, null), [{ type: "tapDown" }, { type: "pick", reason: "unrelated" }]);
    expect(effects[1]).toEqual({ kind: "put", value: "down", reason: "unrelated" });
    expect(state).toEqual({ value: "down", reason: "unrelated", open: null });
  });

  it("dismissing the chips keeps the thumb", () => {
    const { state, effects } = run(initial(null, null), [{ type: "tapDown" }, { type: "dismiss" }]);
    expect(effects[1]).toEqual({ kind: "none" });
    expect(state).toEqual({ value: "down", reason: null, open: null });
  });
});
