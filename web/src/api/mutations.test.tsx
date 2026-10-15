import { QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { useQuery } from "@tanstack/react-query";
import { useSave, useUnreact } from "./mutations";
import { paperQuery } from "./queries";
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
    await waitFor(() => expect(calls.length).toBe(1));
    release();
    await waitFor(() => expect(touched.length).toBe(1));
    expect(calls.map((c) => c.url)).toEqual(["/api/library/2610.00001"]);
    expect(touched).toEqual([["paper", "2610.00001"]]);
  });

  it("stays pending until the paper's page shows the new state", async () => {
    let gets = 0;
    let release!: () => void;
    const held = new Promise<void>((r) => (release = r));
    scriptFetch({
      "PUT /api/library/2610.00001": { message: null, saved: true, grades: {} },
      "GET /api/papers/2610.00001": async () => {
        if (gets++ === 0) return { id: "2610.00001", saved: false };
        await held;
        return { id: "2610.00001", saved: true };
      },
    });
    const client = freshClient();
    const wrapper = ({ children }: { children: React.ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>;
    const { result } = renderHook(() => ({ page: useQuery(paperQuery("2610.00001")), save: useSave("2610.00001") }), { wrapper });
    await waitFor(() => expect(result.current.page.data).toBeTruthy());
    act(() => result.current.save.mutate(true));
    await waitFor(() => expect(gets).toBe(2));
    // The write has returned, the refetch has not: still pending, so a second tap cannot send the same value again.
    expect(result.current.save.isPending).toBe(true);
    release();
    await waitFor(() => expect(result.current.save.isPending).toBe(false));
    expect((result.current.page.data as { saved: boolean }).saved).toBe(true);
  });

  it("is seen as pending by a control that opened after it started", async () => {
    let release!: () => void;
    const held = new Promise<void>((r) => (release = r));
    scriptFetch({
      "PUT /api/library/2610.00001": async () => {
        await held;
        return json({ message: null, saved: true, grades: {} });
      },
    });
    const client = freshClient();
    const wrapper = ({ children }: { children: React.ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>;
    const first = renderHook(() => useSave("2610.00001"), { wrapper });
    act(() => first.result.current.mutate(true));
    first.unmount();
    // Another write's control on the same paper, mounted afterwards, waits for the one in flight.
    const second = renderHook(() => useUnreact("2610.00001"), { wrapper });
    await waitFor(() => expect(second.result.current.isPending).toBe(true));
    const elsewhere = renderHook(() => useUnreact("2610.00002"), { wrapper });
    expect(elsewhere.result.current.isPending).toBe(false);
    release();
    await waitFor(() => expect(second.result.current.isPending).toBe(false));
  });
});
