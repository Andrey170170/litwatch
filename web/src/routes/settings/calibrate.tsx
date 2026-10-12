import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate, useParams, useSearch } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import { ArrowLeft, ArrowRight, Check } from "lucide-react";
import type { Grade, LabelValue } from "@/api/client";
import { sessionQuery, useAnswer, useMore, type Answer, type SessionItem } from "@/api/settings";
import { topicsQuery } from "@/api/queries";
import { useDesktop } from "@/hooks/useDesktop";
import { authorsLine, fmt } from "@/lib/format";
import { topicColour } from "@/lib/topics";
import { cn } from "@/lib/utils";
import { writes } from "@/lib/writeQueue";
import { BTN, BTN_PRIMARY, FIELD, Page } from "@/routes/settings/index";

type FitChoice = "must_read" | "relevant" | "adjacent" | "not_relevant" | "unsure";
type Value = "matters" | "fine" | "not_for_me";

/** One paper's answers as the screen holds them. */
type Draft = {
  fit: Record<string, FitChoice>;
  value: Value | null;
  note: string;
  takes: Record<number, boolean>;
};

function fromItem(item: SessionItem): Draft {
  const fit: Record<string, FitChoice> = {};
  for (const [topic, f] of Object.entries(item.fit ?? {})) fit[topic] = f.value === "relevant" ? (f.grade === "must_read" ? "must_read" : "relevant") : f.value;
  const takes: Record<number, boolean> = {};
  for (const t of item.takes) if (t.accurate !== null) takes[t.id] = t.accurate;
  return { fit, value: item.appraisal?.value ?? null, note: item.appraisal?.note ?? "", takes };
}

function toAnswer(d: Draft): Answer {
  const fit: Record<string, { value: LabelValue; grade?: Grade | null }> = {};
  for (const [topic, c] of Object.entries(d.fit))
    fit[topic] = c === "must_read" || c === "relevant" ? { value: "relevant", grade: c } : { value: c };
  return { fit, value: d.value, note: d.note.trim() || null, takes: Object.fromEntries(Object.entries(d.takes).map(([k, v]) => [k, v])) };
}

/** A Calibrate session: one paper per screen, the questions in the spec's order, answers saved as given. */
export function CalibratePage() {
  const { id } = useParams({ from: "/settings/calibrate/$id" });
  const { at } = useSearch({ from: "/settings/calibrate/$id" });
  const navigate = useNavigate({ from: "/settings/calibrate/$id" });
  const sessionId = Number(id);
  const session = useQuery(sessionQuery(sessionId));
  const topics = useQuery(topicsQuery);
  const watch = useMemo(() => (topics.data ?? []).filter((t) => t.kind === "watch"), [topics.data]);
  const items = session.data?.items ?? [];

  // The position is chosen once, from the route or the first unanswered paper, and moves only
  // when the owner moves it: an answer landing must not jump to the next paper mid-thought.
  const [index, setIndex] = useState<number | null>(at ?? null);
  useEffect(() => {
    if (at !== undefined) setIndex(at);
  }, [at]);
  useEffect(() => {
    if (index === null && session.data) {
      const firstOpen = session.data.items.findIndex((i) => !i.answered_at);
      setIndex(firstOpen === -1 ? session.data.items.length : firstOpen);
    }
  }, [index, session.data]);
  const go = (i: number) => {
    setIndex(i);
    void navigate({ search: { at: i }, replace: true });
  };

  const back = { to: "/settings/quality", label: "Quality" };
  if (session.isPending || index === null) return <Page title="Calibrate" back={back}>{null}</Page>;
  if (session.isError || !session.data) return <Page title="Calibrate" back={back}><p className="text-destructive">No such session.</p></Page>;
  if (items.length === 0) return <Page title="Calibrate" back={back}><p className="text-muted-foreground">Nothing to draw: no Watch topic has graded papers in the last month.</p></Page>;
  const current = Math.min(Math.max(index, 0), items.length);
  if (current >= items.length) return <Done items={items} sessionId={sessionId} go={go} watchNames={Object.fromEntries(watch.map((t) => [t.slug, t.name]))} />;

  const item = items[current]!;
  return (
    <Screen
      key={`${sessionId}-${item.position}`}
      sessionId={sessionId}
      item={item}
      index={current}
      total={items.length}
      watch={watch}
      onBack={current > 0 ? () => go(current - 1) : undefined}
      onNext={() => go(current + 1)}
    />
  );
}

const FIT_CHOICES: { value: FitChoice; word: string }[] = [
  { value: "adjacent", word: "Near it" },
  { value: "not_relevant", word: "Not" },
  { value: "unsure", word: "Unsure" },
];
const VALUE_CHOICES: { value: Value; word: string }[] = [
  { value: "matters", word: "Matters" },
  { value: "fine", word: "Fine" },
  { value: "not_for_me", word: "Not for me" },
];

function Screen({ sessionId, item, index, total, watch, onBack, onNext }: { sessionId: number; item: SessionItem; index: number; total: number; watch: { slug: string; name: string; colour?: string | null }[]; onBack?: () => void; onNext: () => void }) {
  const desktop = useDesktop();
  const [draft, setDraft] = useState<Draft>(() => fromItem(item));
  const [saved, setSaved] = useState<"idle" | "saving" | "saved" | "waiting">(item.answered_at ? "saved" : "idle");
  const answer = useAnswer(sessionId);
  const required = item.topic;
  const ordered = [...watch].sort((a, b) => Number(b.slug === required) - Number(a.slug === required));
  const [abstractOpen, setAbstractOpen] = useState(desktop);

  // The newest draft, what was last sent, and the note's timer. Every write carries the whole
  // answer, so only the newest draft is ever sent, and writes for this paper go out in order.
  const latest = useRef(draft);
  const sent = useRef<string | null>(item.answered_at ? JSON.stringify(toAnswer(fromItem(item))) : null);
  const timer = useRef<number | null>(null);
  const { mutateAsync } = answer;

  const flush = useCallback(() => {
    if (timer.current) window.clearTimeout(timer.current);
    timer.current = null;
    const d = latest.current;
    if (!d.fit[required]) {
      setSaved("waiting");
      return;
    }
    const body = toAnswer(d);
    const json = JSON.stringify(body);
    if (json === sent.current) return;
    sent.current = json;
    setSaved("saving");
    writes.enqueue(`calibrate|${sessionId}|${item.position}`, () => mutateAsync({ position: item.position, answer: body })).then(
      () => {
        if (sent.current === json) setSaved("saved");
      },
      () => {
        if (sent.current === json) {
          sent.current = null;
          setSaved("idle");
        }
      },
    );
  }, [required, sessionId, item.position, mutateAsync]);

  function persist(next: Draft, debounce = false) {
    latest.current = next;
    setDraft(next);
    if (timer.current) window.clearTimeout(timer.current);
    if (debounce) timer.current = window.setTimeout(flush, 800);
    else flush();
  }

  const setNote = (note: string) => persist({ ...latest.current, note }, true);

  // Leaving the paper (Next, Back, the Quality link) sends whatever the timer still held.
  useEffect(() => () => flush(), [flush]);

  // Arrows move between papers unless the note has focus.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.target as HTMLElement)?.tagName === "TEXTAREA") return;
      if (e.key === "ArrowRight") onNext();
      if (e.key === "ArrowLeft" && onBack) onBack();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onNext, onBack]);

  const fitOf = (slug: string) => draft.fit[slug];

  return (
    <div className="mx-auto max-w-[760px] px-4 pt-2 pb-8 desk:px-0 desk:pt-0">
      <div className="flex items-center justify-between gap-2 text-[13px] text-muted-foreground">
        <Link to="/settings/quality" className="hit -ml-2 justify-start gap-1 px-2">
          <ArrowLeft className="size-4" aria-hidden /> Quality
        </Link>
        <span className="tabular">
          {index + 1} of {total}
          <span className="ml-2">
            {saved === "saving" ? "saving…" : saved === "saved" ? <Check className="inline size-3.5 text-success" aria-label="saved" /> : saved === "waiting" ? "fit first" : ""}
          </span>
        </span>
        <span className="text-[12px]">{WHY_WORD[item.why]}</span>
      </div>

      <h1 className="mt-3 font-serif text-[20px] leading-[1.25] font-semibold desk:text-[22px]">
        <Link to="/paper/$" params={{ _splat: item.paper_id }} search={{ topic: item.topic }} className="hover:underline decoration-border">
          {item.title}
        </Link>
      </h1>
      <p className="meta mt-1">
        {authorsLine(item.authors, 5)} · {fmt.published(item.published)}
      </p>
      <div className="mt-2 text-[14px] leading-[1.5] text-body">
        <p className={cn(!abstractOpen && "line-clamp-5")}>{item.abstract}</p>
        {!desktop && (
          <button type="button" className="mt-1 text-[12.5px] text-muted-foreground underline decoration-border" onClick={() => setAbstractOpen((o) => !o)}>
            {abstractOpen ? "less" : "whole abstract"}
          </button>
        )}
      </div>

      {item.takes.length > 0 && (
        <div className="mt-4 space-y-3">
          {item.takes.map((t) => {
            const name = watch.find((w) => w.slug === t.topic)?.name ?? t.topic;
            const colour = topicColour(watch.find((w) => w.slug === t.topic)?.colour);
            return (
              <div key={t.id} className="rounded-lg border border-border px-3.5 py-3">
                <div className="text-[12px] font-medium" style={{ color: colour }}>
                  The take for {name}
                </div>
                <p className="mt-1 font-serif text-[15px] leading-[1.5] text-body">{t.text}</p>
                <div className="mt-2 flex items-center gap-1.5" role="group" aria-label={`Is the take for ${name} accurate?`}>
                  <Chip on={draft.takes[t.id] === true} onClick={() => persist({ ...latest.current, takes: { ...draft.takes, [t.id]: true } })}>
                    Accurate
                  </Chip>
                  <Chip on={draft.takes[t.id] === false} onClick={() => persist({ ...latest.current, takes: { ...draft.takes, [t.id]: false } })}>
                    Off
                  </Chip>
                </div>
              </div>
            );
          })}
        </div>
      )}

      <div className="mt-5 space-y-3">
        {ordered.map((t) => {
          const c = fitOf(t.slug);
          const about = c === "must_read" || c === "relevant";
          return (
            <div key={t.slug} role="group" aria-label={`Fit for ${t.name}`}>
              <div className="mb-1 text-[12.5px] font-medium">
                <span style={{ color: topicColour(t.colour) }}>{t.name}</span>
                {t.slug === required ? <span className="ml-2 font-normal text-muted-foreground">required</span> : null}
              </div>
              <div className="flex flex-wrap gap-1.5">
                <Chip on={about} onClick={() => persist({ ...latest.current, fit: { ...draft.fit, [t.slug]: about ? c! : "relevant" } })}>
                  About it
                </Chip>
                {about && (
                  <>
                    <Chip on={c === "must_read"} small onClick={() => persist({ ...latest.current, fit: { ...draft.fit, [t.slug]: "must_read" } })}>
                      must read
                    </Chip>
                    <Chip on={c === "relevant"} small onClick={() => persist({ ...latest.current, fit: { ...draft.fit, [t.slug]: "relevant" } })}>
                      worth reading
                    </Chip>
                  </>
                )}
                {FIT_CHOICES.map((f) => (
                  <Chip key={f.value} on={c === f.value} onClick={() => persist({ ...latest.current, fit: { ...draft.fit, [t.slug]: f.value } })}>
                    {f.word}
                  </Chip>
                ))}
              </div>
            </div>
          );
        })}

        <div role="group" aria-label="Value to you, whatever the topic">
          <div className="mb-1 text-[12.5px] font-medium">
            To you, whatever the topic <span className="ml-1 font-normal text-muted-foreground">optional</span>
          </div>
          <div className="flex flex-wrap gap-1.5">
            {VALUE_CHOICES.map((v) => (
              <Chip key={v.value} on={draft.value === v.value} onClick={() => persist({ ...latest.current, value: draft.value === v.value ? null : v.value })}>
                {v.word}
              </Chip>
            ))}
          </div>
        </div>

        <label className="block">
          <span className="mb-1 block text-[12.5px] font-medium">
            A note <span className="ml-1 font-normal text-muted-foreground">optional</span>
          </span>
          <textarea className={cn(FIELD, "min-h-[64px]")} value={draft.note} onChange={(e) => setNote(e.target.value)} onBlur={flush} placeholder="Why it matters, or what the take got wrong" />
        </label>
      </div>

      <div className="mt-5 flex items-center justify-between gap-2 border-t border-border pt-3">
        <button type="button" className={BTN} disabled={!onBack} onClick={onBack}>
          <ArrowLeft className="size-4" aria-hidden /> Back
        </button>
        <button type="button" className={index + 1 === total ? BTN_PRIMARY : BTN} onClick={onNext}>
          {index + 1 === total ? "Done" : "Next"} <ArrowRight className="size-4" aria-hidden />
        </button>
      </div>
    </div>
  );
}

const WHY_WORD: Record<SessionItem["why"], string> = {
  above: "shown for the topic",
  below: "dropped for the topic",
  split: "the graders disagreed",
  reaction: "you thumbed it",
};

function Chip({ on, small, onClick, children }: { on?: boolean; small?: boolean; onClick: () => void; children: React.ReactNode }) {
  return (
    <button
      type="button"
      aria-pressed={!!on}
      onClick={onClick}
      className={cn(
        "min-h-[40px] rounded-full border border-border px-3.5 text-[13.5px] hover:bg-muted",
        small && "min-h-[32px] px-3 text-[12.5px]",
        on && "border-foreground bg-foreground text-background hover:bg-foreground",
      )}
    >
      {children}
    </button>
  );
}

/** The end of a session: what it changed, and the way to continue with the papers waiting for a word. */
function Done({ items, sessionId, go, watchNames }: { items: SessionItem[]; sessionId: number; go: (i: number) => void; watchNames: Record<string, string> }) {
  const more = useMore(sessionId);
  const answered = items.filter((i) => i.answered_at);
  const fits: Record<string, number> = {};
  let appraisals = 0;
  let ratings = 0;
  for (const i of answered) {
    for (const [, f] of Object.entries(i.fit ?? {})) fits[f.value] = (fits[f.value] ?? 0) + 1;
    if (i.appraisal) appraisals++;
    ratings += i.takes.filter((t) => t.accurate !== null).length;
  }
  const relevant = answered.filter((i) => i.fit?.[i.topic]?.value === "relevant");
  const dropped = answered.filter((i) => i.why === "below" && i.fit?.[i.topic]?.value === "relevant");
  const unwanted = answered.filter((i) => i.why === "above" && i.fit?.[i.topic]?.value === "not_relevant");
  const matters = answered.filter((i) => i.appraisal?.value === "matters");
  const open = items.filter((i) => !i.answered_at);
  return (
    <Page title="Done" sub={`Session ${sessionId}: ${answered.length} of ${items.length} papers answered.`} back={{ to: "/settings/quality", label: "Quality" }}>
      <ul className="space-y-1 text-[13.5px]">
        <li>
          <b>{Object.values(fits).reduce((a, b) => a + b, 0)}</b> fit answers: {Object.entries(fits).map(([v, n]) => `${n} ${FIT_WORD[v] ?? v}`).join(", ") || "none"}.
        </li>
        <li>
          <b>{appraisals}</b> {appraisals === 1 ? "appraisal" : "appraisals"}, <b>{ratings}</b> take {ratings === 1 ? "rating" : "ratings"}.
        </li>
        {dropped.length > 0 && (
          <li>
            Grading had dropped <b>{dropped.length}</b> you wanted: {dropped.map((i) => i.title).join("; ")}.
          </li>
        )}
        {unwanted.length > 0 && (
          <li>
            Shown but not about the topic: <b>{unwanted.length}</b>: {unwanted.map((i) => i.title).join("; ")}.
          </li>
        )}
        {matters.length > 0 && (
          <li>
            Matters to you: {matters.map((i) => i.title).join("; ")}.
          </li>
        )}
        {relevant.length > 0 && (
          <li className="text-muted-foreground">
            {relevant.length} now in the library for {Array.from(new Set(relevant.map((i) => watchNames[i.topic] ?? i.topic))).join(", ")}.
          </li>
        )}
      </ul>
      {open.length > 0 && (
        <p className="mt-3 text-muted-foreground">
          {open.length} left unanswered; they count nowhere.{" "}
          <button type="button" className="underline decoration-border" onClick={() => go(items.indexOf(open[0]!))}>
            Go back to the first.
          </button>
        </p>
      )}
      <div className="mt-5 flex flex-wrap gap-2">
        <button type="button" className={BTN} disabled={more.isPending} onClick={() => more.mutate(undefined, { onSuccess: (r) => r.added && go(items.length) })}>
          Continue with papers waiting for a word
        </button>
        <Link to="/settings/quality" className={BTN_PRIMARY}>
          Back to Quality
        </Link>
      </div>
    </Page>
  );
}

const FIT_WORD: Record<string, string> = { relevant: "about it", adjacent: "near it", not_relevant: "not", unsure: "unsure" };
