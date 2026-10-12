/**
 * Test helpers: a scripted fetch, a fresh QueryClient, and the real app router on a memory
 * history, so component tests exercise the routes as the browser would.
 */
import { createMemoryHistory, RouterProvider } from "@tanstack/react-router";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render } from "@testing-library/react";
import type { ReactNode } from "react";
import { vi } from "vitest";
import { createAppRouter } from "@/router";

export type Call = { method: string; url: string; body: unknown };

export type Responder = (call: Call) => Promise<Response> | Response | unknown;

/** Replace fetch with a router of responders keyed by "METHOD /path" (query ignored). Returns the call log. */
export function scriptFetch(routes: Record<string, Responder>): Call[] {
  const calls: Call[] = [];
  vi.stubGlobal("fetch", async (input: RequestInfo | URL, init?: RequestInit) => {
    const req = input instanceof Request ? input : new Request(input, init);
    const url = new URL(req.url);
    const text = req.method === "GET" ? null : await req.text();
    const call: Call = { method: req.method, url: url.pathname + url.search, body: text ? JSON.parse(text) : null };
    calls.push(call);
    const key = Object.keys(routes).find((k) => {
      const [m, p] = k.split(" ");
      return m === req.method && url.pathname === p;
    });
    if (!key) return new Response(JSON.stringify({ detail: `unscripted ${call.method} ${call.url}` }), { status: 404 });
    const out = await routes[key]!(call);
    if (out instanceof Response) return out;
    return json(out);
  });
  return calls;
}

export function json(data: unknown, status = 200): Response {
  return new Response(data === undefined ? null : JSON.stringify(data), { status, headers: { "content-type": "application/json" } });
}

/** A promise whose resolution the test controls. */
export function deferred<T>() {
  let resolve!: (v: T) => void;
  let reject!: (e: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

export function freshClient() {
  return new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 }, mutations: { retry: false } } });
}

export function renderWithClient(ui: ReactNode, client = freshClient()) {
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>);
}

export function renderApp(path: string, client = freshClient()) {
  const router = createAppRouter(createMemoryHistory({ initialEntries: [path] }));
  const view = render(
    <QueryClientProvider client={client}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
  return { ...view, router };
}
