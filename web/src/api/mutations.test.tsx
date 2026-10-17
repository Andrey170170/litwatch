import { QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { useQuery } from "@tanstack/react-query";
import { useAnswer, useLabel, useSave, useUnreact } from "./mutations";
import { paperQuery, patchRead, qualityQuery, sessionQuery, sessionsQuery } from "./queries";
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

describe("patchRead", () => {
  type Q = { night: { run: number } };
  const key = qualityQuery("needs").queryKey;
  const wrapperFor = (client: ReturnType<typeof freshClient>) => ({ children }: { children: React.ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>;

  it("starts a first read again when the patch interrupts it, rather than leaving the page loading", async () => {
    let gets = 0;
    let release!: () => void;
    const held = new Promise<void>((r) => (release = r));
    scriptFetch({
      "GET /api/settings/quality": async () => {
        gets++;
        await held;
        return { night: { run: 1 } };
      },
    });
    const client = freshClient();
    const { result } = renderHook(() => useQuery(qualityQuery("needs")), { wrapper: wrapperFor(client) });
    await waitFor(() => expect(gets).toBe(1));
    await act(() => patchRead<Q>(client, key, (q) => ({ night: { run: q.night.run + 1 } })));
    // Nothing to patch yet; the read begun before the write is asked again instead of left cancelled.
    await waitFor(() => expect(gets).toBe(2));
    release();
    await waitFor(() => expect(result.current.data).toEqual({ night: { run: 1 } }));
  });

  it("fetches nothing for a page never asked for", async () => {
    const calls = scriptFetch({ "GET /api/settings/quality": { night: { run: 1 } } });
    const client = freshClient();
    await patchRead<Q>(client, key, (q) => q);
    expect(calls).toEqual([]);
    expect(client.getQueryState(key)).toBeUndefined();
  });
});

describe("an answer in a Calibrate session", () => {
  const item = (answered: boolean) => ({ position: 1, paper_id: "2610.00001", topic: "mi", why: "above", verdict: "related", answer: answered ? "none" : null, answered_at: answered ? "2026-10-15T10:00:00Z" : null, fit: answered ? { mi: "none" } : {}, appraisal: null });
  const row = { id: 3, seed: 7, drawn_at: "2026-10-14T10:00:00Z", papers: 1, answered: 0, differ: 0 };

  it("is seen as pending by a label control on the same paper", async () => {
    let release!: () => void;
    const held = new Promise<void>((r) => (release = r));
    scriptFetch({
      "PUT /api/settings/calibrate/3/items/1": async () => {
        await held;
        return json({ recorded: true });
      },
    });
    const client = freshClient();
    const wrapper = ({ children }: { children: React.ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>;
    const answer = renderHook(() => useAnswer("2610.00001"), { wrapper });
    act(() => answer.result.current.mutate({ sessionId: 3, position: 1, answer: { fit: { mi: "none" }, value: null, note: null } }));
    const label = renderHook(() => useLabel("2610.00001"), { wrapper });
    await waitFor(() => expect(label.result.current.isPending).toBe(true));
    release();
    await waitFor(() => expect(label.result.current.isPending).toBe(false));
  });

  it("patches the session's row in a list whose read was in flight, and the read does not undo it", async () => {
    let answered = false;
    let release!: () => void;
    const held = new Promise<void>((r) => (release = r));
    let listReads = 0;
    scriptFetch({
      "GET /api/settings/calibrate": async () => {
        if (listReads++ === 0) return [row];
        await held;
        return [row]; // begun before the answer: the counts as they were
      },
      "GET /api/settings/calibrate/3": () => ({ id: 3, seed: 7, drawn_at: "2026-10-14T10:00:00Z", items: [item(answered)] }),
      "PUT /api/settings/calibrate/3/items/1": () => {
        answered = true;
        return json({ recorded: true });
      },
    });
    const client = freshClient();
    const wrapper = ({ children }: { children: React.ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>;
    // The session is open (its read held in the cache, as when its screen is shown) and the list behind it.
    const { result } = renderHook(() => ({ list: useQuery(sessionsQuery("calibrate")), session: useQuery(sessionQuery(3)), answer: useAnswer("2610.00001") }), { wrapper });
    await waitFor(() => expect(result.current.list.data).toEqual([row]));
    await waitFor(() => expect(result.current.session.data).toBeTruthy());
    void client.invalidateQueries({ queryKey: sessionsQuery("calibrate").queryKey });
    await waitFor(() => expect(listReads).toBe(2));
    act(() => result.current.answer.mutate({ sessionId: 3, position: 1, answer: { fit: { mi: "none" }, value: null, note: null } }));
    await waitFor(() => expect(result.current.answer.isPending).toBe(false));
    expect(result.current.list.data?.[0]?.answered).toBe(1);
    release();
    await new Promise((r) => setTimeout(r, 20));
    expect(result.current.list.data?.[0]?.answered).toBe(1);
  });
});
