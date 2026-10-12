import { Link, useParams, useRouter, useSearch } from "@tanstack/react-router";
import { ArrowLeft } from "lucide-react";
import { PaperView } from "@/components/PaperView";
import { GearLink } from "@/components/Shell";

/** The paper as a full page, with a back link on the phone. */
export function PaperPage() {
  const { _splat: id = "" } = useParams({ from: "/paper/$" });
  const { topic } = useSearch({ from: "/paper/$" });
  const router = useRouter();
  const canGoBack = router.history.length > 1;
  return (
    <div className="mx-auto max-w-[760px]">
      <nav className="flex items-center justify-between px-4 py-2.5 desk:hidden">
        {canGoBack ? (
          <button type="button" onClick={() => router.history.back()} className="hit -ml-2 justify-start gap-1 px-2 text-[13.5px] text-muted-foreground">
            <ArrowLeft className="size-4" aria-hidden /> Back
          </button>
        ) : (
          <Link to="/" className="hit -ml-2 justify-start gap-1 px-2 text-[13.5px] text-muted-foreground">
            <ArrowLeft className="size-4" aria-hidden /> Digest
          </Link>
        )}
        <GearLink />
      </nav>
      <div className="px-4 pt-2 pb-10 desk:px-7 desk:pt-8">
        <PaperView id={id} surfaceTopic={topic} />
      </div>
    </div>
  );
}
