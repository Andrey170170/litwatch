/** Integrations (DESIGN.md, "Integrations"): a card per integration with its status and the failing event's
 * actions, the outbox counts under them. An integration's events wait on an endpoint. */
import { useQuery } from "@tanstack/react-query";
import { Link, useParams } from "@tanstack/react-router";
import { useState } from "react";
import type { Schemas } from "@/api/client";
import { useRetryIntegration, useSkipEvent } from "@/api/mutations";
import { integrationsQuery } from "@/api/queries";
import { dateTime } from "@/lib/format";
import { Later, Load, PageHead, PaperLink } from "./parts";

type Card = Schemas["IntegrationCard"];

function status(c: Card): string {
  if (!c.enabled) return "Off. Turn it on in litwatch.toml.";
  if (c.failing) return `1 event failed after ${c.failing.failures} ${c.failing.failures === 1 ? "try" : "tries"}${c.failing.retry_at ? ` · next try ${dateTime(c.failing.retry_at)}` : ""}`;
  if (c.lag) return `${c.lag} ${c.lag === 1 ? "event" : "events"} waiting`;
  return `Up to date${c.skipped ? ` · ${c.skipped} skipped` : ""}`;
}

function IntegrationCard({ c, done, settle }: { c: Card; done: string | undefined; settle: (what: string) => void }) {
  const retry = useRetryIntegration();
  const skip = useSkipEvent();
  const failing = c.enabled && c.failing && !done;
  return (
    <div className={`icard${failing ? " fail" : ""}${c.enabled ? "" : " off"}`}>
      <div className="nh">
        <span className="mono in">{c.name}</span>
        <span className="faint">{c.enabled ? "on" : "off"}</span>
      </div>
      {c.description && <div className="nb">{c.description}</div>}
      <div className="ist">{done ?? status(c)}</div>
      {failing && c.failing && (
        <>
          <div className="ierr mono">
            {c.failing.type}
            {c.failing.paper_id && (
              <>
                {" "}
                <PaperLink id={c.failing.paper_id} className="inl">
                  {c.failing.paper_id}
                </PaperLink>
              </>
            )}
            {c.failing.error ? ` → ${c.failing.error}` : ""}
          </div>
          <div className="nact">
            <button className="btn" disabled={retry.isPending} onClick={() => retry.mutate(c.name)}>
              Retry now
            </button>
            <button className="btn" disabled={skip.isPending} onClick={() => skip.mutate({ name: c.name, eventId: c.failing!.event_id }, { onSuccess: () => settle("Event skipped · up to date") })}>
              Skip this event
            </button>
            <Link to="/settings/integrations/$name" params={{ name: c.name }} className="btn">
              All events
            </Link>
          </div>
        </>
      )}
      {c.enabled && !failing && (
        <div className="nact">
          <Link to="/settings/integrations/$name" params={{ name: c.name }} className="btn">
            All events
          </Link>
        </div>
      )}
    </div>
  );
}

export function IntegrationsPage() {
  const cards = useQuery(integrationsQuery);
  const [settled, setSettled] = useState<Record<string, string>>({});
  return (
    <>
      <PageHead title="Integrations" sub="Each reacts to the pipeline's events on its own schedule. Which are loaded is set in litwatch.toml." />
      <Load error={cards.isError} retry={() => void cards.refetch()} loading={cards.isPending} />
      {cards.data?.map((c) => (
        <IntegrationCard key={c.name} c={c} done={settled[c.name]} settle={(what) => setSettled((x) => ({ ...x, [c.name]: what }))} />
      ))}
      {cards.data && !cards.data.length && <div className="empty">No integration is configured.</div>}
      {cards.data && cards.data.length > 0 && (
        <>
          <div className="cap">Outbox</div>
          <div className="mtbl c4">
            <div className="mh">
              <span>Integration</span>
              <span>Waiting</span>
              <span>Failed</span>
              <span>Skipped</span>
            </div>
            {cards.data.map((c) => (
              <div className="mr" key={c.name}>
                <span className="mono">{c.name}</span>
                <span className={`mono${c.lag ? "" : " faint"}`}>{c.lag}</span>
                <span className={`mono ${c.failing && !settled[c.name] ? "badt" : "faint"}`}>{c.failing && !settled[c.name] ? 1 : 0}</span>
                <span className={`mono${c.skipped ? "" : " faint"}`}>{c.skipped}</span>
              </div>
            ))}
          </div>
        </>
      )}
    </>
  );
}

/** An integration's events: the page is here, its list waits on an events endpoint. */
export function EventsPage() {
  const { name } = useParams({ from: "/settings/integrations/$name" });
  const cards = useQuery(integrationsQuery);
  const c = cards.data?.find((x) => x.name === name);
  return (
    <div className="setpg">
      <PageHead title={`${name} · events`} sub={c?.description ?? undefined} deeper />
      {c && <div className="ist mb-3">{status(c)}</div>}
      <Later>The list of events (time, event, paper, result; all, failed or waiting) comes with an events endpoint on the outbox. Until then the failing event shows on the integration's card.</Later>
    </div>
  );
}
