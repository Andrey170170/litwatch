/**
 * Where a shared or looked-up arXiv link lands (DESIGN.md "Papers", the share target): the
 * paper as a card that opens it, then one large button per thing the owner can say about
 * it: Save, "Missed by <Watch topic>" where it is not already relevant there, and
 * "Important for <field>" for each Radar or Pulse topic. Each shows its done state and
 * can be taken back. Phone first: every control is a full-width row of at least 44px.
 */
import { Link } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import { Check } from "lucide-react";
import { api, ok, type PaperTopic, type TopicCard } from "@/api/client";
import { useImportant, useMiss, useSave, useUnimportant, useUnlabel } from "@/api/mutations";
import { paperQuery } from "@/api/queries";
import { GradeDot } from "@/components/paper-bits";
import { authorsLine, fmt } from "@/lib/format";
import { topicColour } from "@/lib/topics";
import { cn } from "@/lib/utils";

export function ShareResult({ q, topics }: { q: string; topics: TopicCard[] }) {
  const look = useQuery({
    queryKey: ["lookup", q],
    queryFn: async () => ok(await api.POST("/api/lookup", { body: { q } })),
    retry: false,
  });
  const id = look.data?.row.id;
  // The paper's own page carries what each topic says of it: grades, labels, memberships.
  const paper = useQuery({ ...paperQuery(id ?? ""), enabled: !!id });
  return (
    <section className="mt-3 rounded-[12px] border border-border bg-muted px-4 py-3.5" aria-label="Shared paper">
      {look.isPending && <p className="meta">Looking up {q}…</p>}
      {look.isError && <p className="text-[14px] text-destructive">Nothing found for “{q}”. An arXiv id or link is what works here.</p>}
      {look.data && (
        <>
          <p className="meta text-[11.5px] tracking-[.06em] uppercase">{look.data.fetched ? "Fetched into the collection just now" : "Already in the collection"}</p>
          <Link to="/paper/$" params={{ _splat: look.data.row.id }} search={{ topic: "none" }} className="mt-1.5 block rounded-[10px] border border-border bg-background px-3.5 py-3 hover:border-foreground">
            <div className="grid grid-cols-[14px_1fr] gap-x-2.5">
              <div className="pt-[7px]">
                <GradeDot grade={look.data.row.grade} grading={look.data.row.grading} />
              </div>
              <div>
                <h3 className="font-serif text-[17px] leading-[1.3] font-semibold">{look.data.row.title}</h3>
                <p className="meta mt-0.5">
                  {authorsLine(look.data.row.authors)} · {fmt.cell(look.data.row.published)}
                </p>
                {look.data.row.take && <p className="mt-1 line-clamp-3 text-[13.5px] text-body">{look.data.row.take}</p>}
                <p className="mt-1.5 text-[13px] font-semibold text-primary">Open the paper</p>
              </div>
            </div>
          </Link>
          {paper.data && <Actions paperId={paper.data.id} saved={!!paper.data.saved} states={paper.data.topics} topics={topics} />}
        </>
      )}
    </section>
  );
}

/** The rows of actions, each a big button that reads as done once it is. */
function Actions({ paperId, saved, states, topics }: { paperId: string; saved: boolean; states: PaperTopic[]; topics: TopicCard[] }) {
  const save = useSave(paperId);
  const miss = useMiss();
  const unlabel = useUnlabel(paperId);
  const important = useImportant();
  const unimportant = useUnimportant();
  const state = (slug: string) => states.find((t) => t.slug === slug);
  const active = topics.filter((t) => t.status === "active");
  const watch = active.filter((t) => t.kind === "watch");
  const fields = active.filter((t) => t.kind !== "watch");
  return (
    <div className="mt-3 grid gap-2">
      <ActionRow
        done={saved}
        pending={save.isPending}
        label="Save to the library"
        doneLabel="Saved"
        onDo={() => save.mutate(true)}
        onUndo={() => save.mutate(false)}
        undoLabel="Unsave"
      />
      {watch.map((t) => {
        const st = state(t.slug);
        const relevant = st?.grade === "must_read" || st?.grade === "relevant";
        // `missed` is the API's word: a miss stands for this topic, whatever labelled it before.
        const reported = !!st?.missed;
        if (relevant && !reported)
          return <p key={t.slug} className="px-1 text-[13px] text-muted-foreground">Already relevant for {t.name}.</p>;
        return (
          <ActionRow
            key={t.slug}
            colour={topicColour(t.colour)}
            done={reported}
            pending={miss.isPending || unlabel.isPending}
            label={`Missed by ${t.name}`}
            doneLabel={`Reported as missed by ${t.name}`}
            onDo={() => miss.mutate({ paper_id: paperId, topic: t.slug })}
            onUndo={() => unlabel.mutate(t.slug)}
            undoLabel="Take back"
          />
        );
      })}
      {fields.map((t) => {
        const st = state(t.slug);
        // Done means the owner's own addition; a member triage found, or a plain thumb, is not.
        const done = !!st?.by_owner;
        const note = done ? `in the field as your addition; ${saved ? "saved" : "not in the library"}` : st?.member ? "already in the field by triage" : undefined;
        return (
          <ActionRow
            key={t.slug}
            colour={topicColour(t.colour)}
            done={done}
            pending={important.isPending || unimportant.isPending}
            label={`Important for ${t.name}`}
            doneLabel={`Important for ${t.name}`}
            note={note}
            onDo={() => important.mutate({ paper_id: paperId, topic: t.slug })}
            onUndo={() => unimportant.mutate({ paper_id: paperId, topic: t.slug })}
            undoLabel="Take back"
          />
        );
      })}
    </div>
  );
}

type RowProps = {
  label: string;
  doneLabel: string;
  undoLabel: string;
  done: boolean;
  pending: boolean;
  note?: string;
  colour?: string;
  onDo: () => void;
  onUndo: () => void;
};

/** One action: a full-width button; once done, a check, the done words, and a small undo. */
function ActionRow({ label, doneLabel, undoLabel, done, pending, note, colour, onDo, onUndo }: RowProps) {
  if (!done)
    return (
      <button
        type="button"
        disabled={pending}
        onClick={onDo}
        className="flex min-h-[46px] w-full items-center gap-3 rounded-[10px] border border-border bg-background px-3.5 text-left text-[15px] hover:border-foreground disabled:opacity-50"
      >
        {colour && <span className="size-2.5 shrink-0 rounded-full" style={{ background: colour }} aria-hidden />}
        <span className="min-w-0 flex-1">
          {label}
          {note && <span className="block text-[12.5px] text-muted-foreground">{note}</span>}
        </span>
      </button>
    );
  return (
    <div className={cn("flex min-h-[46px] w-full items-center gap-3 rounded-[10px] border border-foreground bg-background px-3.5 text-[15px]")}>
      <Check className="size-[18px] shrink-0 text-success" strokeWidth={2.5} aria-hidden />
      <span className="min-w-0 flex-1">
        <span className="font-semibold">{doneLabel}</span>
        {note && <span className="block text-[12.5px] font-normal text-muted-foreground">{note}</span>}
      </span>
      <button
        type="button"
        disabled={pending}
        onClick={onUndo}
        aria-label={`${undoLabel}: ${doneLabel}`}
        className="hit shrink-0 text-[13px] text-muted-foreground underline decoration-border hover:text-foreground disabled:opacity-50"
      >
        {undoLabel}
      </button>
    </div>
  );
}
