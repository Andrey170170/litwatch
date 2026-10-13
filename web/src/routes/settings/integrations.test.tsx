import { render, screen, waitFor } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { freshClient, json, renderApp, scriptFetch } from "@/test/harness";
import { IntegrationView } from "@/routes/settings/plain";

afterEach(() => vi.unstubAllGlobals());

const status = { state: "quiet", read: 1 };
const failing = {
  name: "notion",
  description: "POSTs events to a URL.",
  enabled: true,
  lag: 3,
  failing: { event_id: 42, type: "grade.changed", paper_id: "2610.00001", failures: 2, retry_at: "2026-10-12T07:00:00Z", error: "RuntimeError: the webhook answered HTTP 500" },
  last_error: "RuntimeError: the webhook answered HTTP 500",
  failed_at: "2026-10-12T06:30:00Z",
  skipped: 1,
  answers_card_requests: false,
  search: null,
};
const archive = { name: "archive", description: "Registers papers.", enabled: true, lag: 0, failing: null, last_error: null, failed_at: null, skipped: 0, answers_card_requests: false, search: { queries: 12, left_out: 1, last_error: "no answer within 5s", last_error_at: "2026-10-12T06:00:00Z" } };

describe("Settings → Integrations", () => {
  it("shows a failing card with its error, retries at once, and skips on a second click", async () => {
    const calls = scriptFetch({
      "GET /api/status": () => json(status),
      "GET /api/settings/integrations": () => json([failing, archive]),
      "POST /api/settings/integrations/notion/retry": () => new Response(null, { status: 204 }),
      "POST /api/settings/integrations/notion/skip": () => json({ event_id: 42, type: "grade.changed", paper_id: "2610.00001", failures: 2 }),
    });
    const user = userEvent.setup();
    renderApp("/settings/integrations");
    expect(await screen.findByText(/stuck on grade.changed for 2610.00001, 2 tries/)).toBeInTheDocument();
    expect(screen.getByText(/the webhook answered HTTP 500/)).toBeInTheDocument();
    expect(screen.getByText(/1 skipped/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Retry now" }));
    await waitFor(() => expect(calls.some((c) => c.url.endsWith("/notion/retry"))).toBe(true));
    await user.click(screen.getByRole("button", { name: "Skip this event" }));
    expect(calls.some((c) => c.url.endsWith("/notion/skip"))).toBe(false);
    await user.click(screen.getByRole("button", { name: "Skip it, really" }));
    await waitFor(() => expect(calls.some((c) => c.url.endsWith("/notion/skip"))).toBe(true));
    // The search provider's statistics.
    expect(screen.getByText(/Searches since start: 12, left out 1/)).toBeInTheDocument();
    expect(screen.getByText("up to date")).toBeInTheDocument();
  });

  it("disarms the skip when the failing event changes under it", async () => {
    scriptFetch({});
    const user = userEvent.setup();
    const client = freshClient();
    const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>;
    const view = render(<IntegrationView c={failing} />, { wrapper });
    await user.click(screen.getByRole("button", { name: "Skip this event" }));
    expect(screen.getByRole("button", { name: "Skip it, really" })).toBeInTheDocument();
    view.rerender(<IntegrationView c={{ ...failing, failing: { ...failing.failing, event_id: 43, type: "paper.saved" } }} />);
    expect(screen.getByRole("button", { name: "Skip this event" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Skip it, really" })).not.toBeInTheDocument();
  });

  it("points to the docs when nothing is configured", async () => {
    scriptFetch({ "GET /api/status": () => json(status), "GET /api/settings/integrations": () => json([]) });
    renderApp("/settings/integrations");
    expect(await screen.findByText(/None configured/)).toBeInTheDocument();
    expect(screen.getByText("docs/integrations.md")).toBeInTheDocument();
  });
});
