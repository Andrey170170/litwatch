import { useEffect, useState } from "react";
import { ThumbsDown, ThumbsUp } from "lucide-react";
import type { ReactionReason, ReactionValue } from "@/api/client";
import { useReact, useUnreact } from "@/api/mutations";
import { DOWN_REASONS, initial, reduce, type ThumbState } from "@/lib/thumbs";
import { cn } from "@/lib/utils";

type Props = {
  paperId: string;
  /** The surface's topic, or null on a plain search result. */
  topic: string | null;
  value: ReactionValue | null | undefined;
  reason: ReactionReason | null | undefined;
  className?: string;
};

/**
 * Two 16px glyphs in 40px hit areas, quiet until used (DESIGN.md "Thumbs"). The reason chips
 * open under the row; the first tap on one closes them.
 */
export function Thumbs({ paperId, topic, value, reason, className }: Props) {
  const [state, setState] = useState<ThumbState>(() => initial(value, reason));
  const react = useReact(paperId);
  const unreact = useUnreact(paperId);

  // The server's view wins when it changes under us (another surface reacted).
  useEffect(() => {
    setState((s) => (s.value === (value ?? null) && s.reason === (reason ?? null) ? s : initial(value, reason)));
  }, [value, reason]);

  function dispatch(action: Parameters<typeof reduce>[1]) {
    const [next, effect] = reduce(state, action);
    setState(next);
    if (effect.kind === "put") react.mutate({ topic, value: effect.value, reason: effect.reason });
    else if (effect.kind === "delete") unreact.mutate(topic);
  }

  const up = state.value === "up";
  const down = state.value === "down";

  return (
    <div className={cn("flex flex-col items-end", className)}>
      <div className="flex items-center" role="group" aria-label="Your reaction">
        <button
          type="button"
          className={cn("hit rounded-full text-muted-foreground hover:text-foreground", up && "text-foreground")}
          aria-pressed={up}
          aria-label={up ? "Thumb up, set. Tap again for Great find, or clear" : "Thumb up"}
          onClick={() => dispatch({ type: "tapUp" })}
        >
          <span className={cn("grid size-7 place-items-center rounded-full", up && "border border-border bg-muted")}>
            <ThumbsUp className="size-4" strokeWidth={1.75} fill={up ? "currentColor" : "none"} aria-hidden />
          </span>
        </button>
        <button
          type="button"
          className={cn("hit rounded-full text-muted-foreground hover:text-foreground", down && "text-foreground")}
          aria-pressed={down}
          aria-label={down ? "Thumb down, set. Tap again to clear" : "Thumb down"}
          onClick={() => dispatch({ type: "tapDown" })}
        >
          <span className={cn("grid size-7 place-items-center rounded-full", down && "border border-border bg-muted")}>
            <ThumbsDown className="size-4" strokeWidth={1.75} fill={down ? "currentColor" : "none"} aria-hidden />
          </span>
        </button>
      </div>
      {state.open === "reasons" && (
        <Chips
          label="Why?"
          options={DOWN_REASONS}
          onPick={(r) => dispatch({ type: "pick", reason: r })}
          onDismiss={() => dispatch({ type: "dismiss" })}
        />
      )}
      {state.open === "great" && (
        <Chips
          options={[{ reason: "great_find", label: "Great find" }]}
          onPick={(r) => dispatch({ type: "pick", reason: r })}
          onDismiss={() => dispatch({ type: "dismiss" })}
        />
      )}
      {state.reason && state.open === null && (
        <span className="meta mt-0.5 text-[12px]">{labelFor(state.reason)}</span>
      )}
    </div>
  );
}

function labelFor(reason: ReactionReason): string {
  return reason === "great_find" ? "Great find" : (DOWN_REASONS.find((r) => r.reason === reason)?.label ?? reason);
}

function Chips({
  label,
  options,
  onPick,
  onDismiss,
}: {
  label?: string;
  options: { reason: ReactionReason; label: string }[];
  onPick: (r: ReactionReason) => void;
  onDismiss: () => void;
}) {
  return (
    <div
      className="animate-in fade-in mt-1.5 flex max-w-full flex-wrap items-center justify-end gap-1.5 rounded-xl border border-border bg-popover p-2 text-[13px] shadow-[0_6px_20px_rgba(0,0,0,.08)]"
      role="group"
      aria-label={label ?? "Great find"}
      onKeyDown={(e) => e.key === "Escape" && onDismiss()}
    >
      {label && <span className="meta px-1">{label}</span>}
      {options.map((o) => (
        <button
          key={o.reason}
          type="button"
          autoFocus={o === options[0]}
          className="min-h-[36px] rounded-full border border-border px-3 hover:bg-muted"
          onClick={() => onPick(o.reason)}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}
