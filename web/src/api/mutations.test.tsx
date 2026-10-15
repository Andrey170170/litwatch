import { QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { useSave } from "./mutations";
import { freshClient, json, scriptFetch } from "@/test/harness";

describe("a write on a paper", () => {
  it("settles the paper it was sent for, even when the page has moved to another", async () => {
    let release!: () => void;
    const held = new Promise<void>((r) => (release = r));
    const calls = scriptFetch({
      "PUT /api/library/2610.00001": async () => {
        await held;
        return json({ message: null, saved: true, grades: {} });
      },
    });
    const client = freshClient();
    const touched: string[][] = [];
    client.invalidateQueries = ((filters: { queryKey?: readonly unknown[] }) => {
      if (filters.queryKey?.[0] === "paper") touched.push(filters.queryKey as string[]);
      return Promise.resolve();
    }) as typeof client.invalidateQueries;
    const wrapper = ({ children }: { children: React.ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>;
    const { result, rerender } = renderHook(({ id }) => useSave(id), { wrapper, initialProps: { id: "2610.00001" } });
    act(() => result.current.mutate(true));
    rerender({ id: "2610.00002" });
    await waitFor(() => expect(result.current.isPending).toBe(true));
    release();
    await waitFor(() => expect(result.current.isPending).toBe(false));
    expect(calls.map((c) => c.url)).toEqual(["/api/library/2610.00001"]);
    expect(touched).toEqual([["paper", "2610.00001"]]);
  });
});
