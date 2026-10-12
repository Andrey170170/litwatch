import { Link } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import { statusQuery } from "@/api/queries";
import { statusLine, type Tone } from "@/lib/status";
import { cn } from "@/lib/utils";

export const TONE_DOT: Record<Tone, string> = {
  grey: "bg-adjacent",
  red: "bg-destructive",
  amber: "bg-warning",
  green: "bg-success",
};

/** The dot and sentence about the latest run. Always opens Settings → Quality. */
export function StatusText({ className, uppercase }: { className?: string; uppercase?: boolean }) {
  const { data } = useQuery(statusQuery);
  if (!data) return <span className={cn("inline-block h-[1em] w-24", className)} />;
  const line = statusLine(data);
  return (
    <Link
      to="/settings/quality"
      className={cn(
        "inline-flex items-center gap-1.5 whitespace-nowrap text-muted-foreground hover:text-foreground",
        uppercase && "text-[11px] uppercase tracking-[.07em]",
        className,
      )}
      aria-label={`Status: ${line.text}. Open Quality`}
    >
      <span aria-hidden className={cn("inline-block size-[7px] shrink-0 rounded-full", TONE_DOT[line.tone])} />
      {line.text}
    </Link>
  );
}

/** The chip in the header, everywhere the masthead is not. */
export function StatusChip({ className }: { className?: string }) {
  return (
    <StatusText className={cn("rounded-full border border-border bg-card px-2.5 py-[3px] text-[12.5px]", className)} />
  );
}
