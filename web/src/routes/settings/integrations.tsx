/** Integrations (DESIGN.md, "Integrations"): a card per integration with its status and the failing event's
 * actions, the outbox counts under them. An integration's events wait on an endpoint. */
import { useQuery } from "@tanstack/react-query";
import { Link, useParams } from "@tanstack/react-router";
import { useState } from "react";
import type { Schemas } from "@/api/client";
import { useRetryIntegration, useSkipEvent } from "@/api/mutations";
import { integrationsQuery } from "@/api/queries";
import { dateTime, plural } from "@/lib/format";
import { Later, Load, PageHead, PaperLink } from "./parts";

type Card = Schemas["IntegrationCard"];

function status(c: Card): string {
  if (!c.enabled) return "Off. Turn it on in litwatch.toml.";
  if (c.failing) return `1 event failed after ${plural(c.failing.failures, "try", "tries")}${c.failing.retry_at ? ` · next try ${dateTime(c.failing.retry_at)}` : ""}`;
  if (c.lag) return `${plural(c.lag, "event")} waiting`;
  return `Up to date${c.skipped ? ` · ${c.skipped} skipped` : ""}`;
}

/** Whether the card's failing event was skipped on this visit: the card keeps saying so after the refetch
 * clears it, until another event fails. */
const skippedOn = (c: Card, skipped: Map<string, number>) => skipped.has(c.name) && (!c.failing || c.failing.event_id === skipped.get(c.name));

function IntegrationCard({ c, skipped, onSkip }: { c: Card; skipped: Map<string, number>; onSkip: (eventId: number) => void }) {
  const retry = useRetryIntegration();
  const skip = useSkipEvent();
  const done = skippedOn(c, skipped);
  const f = c.enabled && !done ? c.failing : null;
  return (
    <div className={`icard${f ? " fail" : ""}${c.enabled ? "" : " off"}`}>
      <div className="nh">
        <span className="mono in">{c.name}</span>
        <span className="faint">{c.enabled ? "on" : "off"}</span>
      </div>
      {c.description && <div className="nb">{c.description}</div>}
      <div className="ist">{done ? "Event skipped · up to date" : status(c)}</div>
      {f && (
        <div className="ierr mono">
          {f.type}
          {f.paper_id && (
            <>
              {" "}
              <PaperLink id={f.paper_id} className="inl">
                {f.paper_id}
              </PaperLink>
            </>
          )}
          {f.error ? ` → ${f.error}` : ""}
        </div>
      )}
      {c.enabled && (
        <div className="nact">
          {f && (
            <>
              <button className="btn" disabled={retry.isPending} onClick={() => retry.mutate(c.name)}>
                Retry now
              </button>
              <button className="btn" disabled={skip.isPending} onClick={() => skip.mutate({ name: c.name, eventId: f.event_id }, { onSuccess: () => onSkip(f.event_id) })}>
                Skip this event
              </button>
            </>
          )}
          <Link to="/settings/integrations/$name" params={{ name: c.name }} className="btn">
            All events
          </Link>
        </div>
      )}
    </div>
  );
}

export function IntegrationsPage() {
  const cards = useQuery(integrationsQuery("integrations"));
  // The event skipped on this visit per integration: each card keeps its result in place until the page is left.
  const [skipped, setSkipped] = useState<Map<string, number>>(new Map());
  return (
    <>
      <PageHead title="Integrations" sub="Each reacts to the pipeline's events on its own schedule. Which are loaded is set in litwatch.toml." />
      <Load q={cards} />
      {cards.data?.map((c) => (
        <IntegrationCard key={c.name} c={c} skipped={skipped} onSkip={(id) => setSkipped((x) => new Map(x).set(c.name, id))} />
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
            {cards.data.map((c) => {
              const failed = c.failing && !skippedOn(c, skipped) ? 1 : 0;
              return (
              <div className="mr" key={c.name}>
                <span className="mono">{c.name}</span>
                <span className={`mono${c.lag ? "" : " faint"}`}>{c.lag}</span>
                <span className={`mono ${failed ? "badt" : "faint"}`}>{failed}</span>
                <span className={`mono${c.skipped ? "" : " faint"}`}>{c.skipped}</span>
              </div>
              );
            })}
          </div>
        </>
      )}
    </>
  );
}

/** An integration's events: the page is here, its list waits on an events endpoint. */
export function EventsPage() {
  const { name } = useParams({ from: "/settings/integrations/$name" });
  const cards = useQuery(integrationsQuery(`integrations/${name}`));
  const c = cards.data?.find((x) => x.name === name);
  return (
    <div className="setpg">
      <PageHead title={`${name} · events`} sub={c?.description ?? undefined} deeper />
      {c && <div className="ist mb-3">{status(c)}</div>}
      <Later>The list of events (time, event, paper, result; all, failed or waiting) comes with an events endpoint on the outbox. Until then the failing event shows on the integration's card.</Later>
    </div>
  );
}
