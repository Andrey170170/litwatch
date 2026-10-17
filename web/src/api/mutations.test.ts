import { QueryClient } from "@tanstack/react-query";
import { describe, expect, it } from "vitest";
import { restripe } from "./mutations";

const topic = { slug: "cot", name: "CoT", depth: "focus" as const, parent: null, color: "#0ff" };
const square = (id: number, state: "written" | "failed" | "nothing_new") => ({ id, day: "2026-10-14", state, best_rarity: state === "written" ? ("rare" as const) : null });

describe("a digest written again", () => {
  it("recolours its square in the Topics tab and the topic's page, in place", async () => {
    const qc = new QueryClient();
    qc.setQueryData(["topics"], [{ depth: "focus", topics: [{ ...topic, strip: [square(1, "written"), square(2, "failed")] }, { ...topic, slug: "other", strip: [square(2, "failed")] }] }]);
    qc.setQueryData(["topic", "cot", {}, "top", 50], { pages: [{ topic: { ...topic, strip: [square(2, "failed")] }, rows: [] }], pageParams: [0] });
    await restripe(qc, { id: 2, state: "written", best_rarity: "epic", topic: topic as never });
    const groups = qc.getQueryData<{ topics: { slug: string; strip: { id: number; state: string; best_rarity: string | null }[] }[] }[]>(["topics"])!;
    expect(groups[0]!.topics[0]!.strip).toEqual([square(1, "written"), { ...square(2, "written"), best_rarity: "epic" }]);
    // another topic's square with the same id (there is none, but the patch is by topic too) is left alone
    expect(groups[0]!.topics[1]!.strip).toEqual([square(2, "failed")]);
    const page = qc.getQueryData<{ pages: { topic: { strip: unknown[] } }[] }>(["topic", "cot", {}, "top", 50])!;
    expect(page.pages[0]!.topic.strip).toEqual([{ ...square(2, "written"), best_rarity: "epic" }]);
  });
});
