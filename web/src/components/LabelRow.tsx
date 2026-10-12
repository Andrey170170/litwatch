import type { LabelValue } from "@/api/client";
import { useLabel, useUnlabel } from "@/api/mutations";
import { cn } from "@/lib/utils";

const CHOICES: { value: LabelValue; word: string }[] = [
  { value: "relevant", word: "Relevant" },
  { value: "adjacent", word: "Adjacent" },
  { value: "not_relevant", word: "Not relevant" },
];

/** The owner's word on a paper for a Watch topic: three chips, the current one lit, tap again to take it back. */
export function LabelRow({ paperId, topic, current, lead = "Your call, if you want to make one:" }: { paperId: string; topic: string; current: LabelValue | null | undefined; lead?: string }) {
  const label = useLabel(paperId);
  const unlabel = useUnlabel(paperId);
  return (
    <div className="flex flex-wrap items-center gap-1.5 text-[13px]">
      {lead && <span className="text-muted-foreground">{lead}</span>}
      {CHOICES.map((c) => (
        <button
          key={c.value}
          type="button"
          aria-pressed={current === c.value}
          disabled={label.isPending || unlabel.isPending}
          onClick={() => (current === c.value ? unlabel.mutate(topic) : label.mutate({ topic, value: c.value }))}
          className={cn(
            "min-h-[36px] rounded-full border border-border px-3 hover:bg-muted",
            current === c.value && "border-foreground bg-foreground text-background hover:bg-foreground",
          )}
        >
          {c.word}
        </button>
      ))}
      {current === "unsure" && <span className="text-muted-foreground">(you said unsure)</span>}
    </div>
  );
}
