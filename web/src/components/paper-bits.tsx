/** Small pieces shared by entries, rows and the paper page. */
import { Link } from "@tanstack/react-router";
import type { Grade } from "@/api/client";
import { bibtexFor, copyText, useSave } from "@/api/mutations";
import { topicColour } from "@/lib/topics";
import { cn } from "@/lib/utils";

/** The grade dot on a row (DESIGN.md "Row"): never with a word beside it. */
export function GradeDot({ grade, grading, className }: { grade: Grade | null | undefined; grading?: boolean; className?: string }) {
  if (grading && !grade) return <span className={cn("meta text-[12px]", className)}>grading</span>;
  const look =
    grade === "must_read"
      ? "bg-foreground"
      : grade === "relevant"
        ? "bg-success"
        : grade === "adjacent"
          ? "bg-adjacent"
          : "border border-border bg-transparent";
  return (
    <span
      className={cn("inline-block size-[8px] shrink-0 rounded-full", look, className)}
      aria-label={grade ? GRADE_WORD[grade] : "no topic's candidate"}
      role="img"
    />
  );
}

export const GRADE_WORD: Record<Grade, string> = {
  must_read: "must read",
  relevant: "relevant",
  adjacent: "adjacent",
  none: "not relevant",
};

export function TopicChip({ slug, name, colour, status }: { slug: string; name: string; colour?: string | null; status?: string }) {
  const c = topicColour(colour, status);
  return (
    <Link
      to="/topics/$slug"
      params={{ slug }}
      className="inline-flex items-center rounded-full border px-2.5 py-[1px] text-[12.5px] font-medium whitespace-nowrap"
      style={{ borderColor: c, color: c }}
    >
      {name}
    </Link>
  );
}

/** Save / Saved, one toggle. As a link in an action row, as a button on the paper page. */
export function SaveToggle({ paperId, saved, as = "link" }: { paperId: string; saved: boolean; as?: "link" | "button" }) {
  const save = useSave(paperId);
  const onClick = () => save.mutate(!saved);
  if (as === "button")
    return (
      <button
        type="button"
        onClick={onClick}
        disabled={save.isPending}
        className={cn(
          "min-h-[40px] rounded-full border px-4 text-[13.5px]",
          saved ? "border-success text-success" : "border-foreground bg-foreground text-background",
        )}
        aria-pressed={saved}
      >
        {saved ? "Saved" : "Save"}
      </button>
    );
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={save.isPending}
      className={cn("py-1.5 hover:text-foreground", saved && "font-semibold text-success")}
      aria-pressed={saved}
    >
      {saved ? "Saved" : "Save"}
    </button>
  );
}

export function arxivUrl(id: string) {
  return `https://arxiv.org/abs/${id}`;
}
export function pdfUrl(id: string) {
  return `https://arxiv.org/pdf/${id}`;
}

export function BibtexCopy({ ids, as = "link" }: { ids: string[]; as?: "link" | "button" }) {
  const copy = async () => copyText(await bibtexFor(ids), "BibTeX copied");
  if (as === "button")
    return (
      <button type="button" onClick={copy} className="min-h-[40px] rounded-full border border-border px-4 text-[13.5px] hover:bg-muted">
        Copy BibTeX
      </button>
    );
  return (
    <button type="button" onClick={copy} className="py-1.5 hover:text-foreground">
      BibTeX
    </button>
  );
}
