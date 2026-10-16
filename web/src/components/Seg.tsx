/** A segmented control: one button per value, the chosen one filled; `auto` underlines the pipeline's own while
 * nothing is chosen. With `clear`, picking the chosen value again clears it. */
import type { Schemas } from "@/api/client";

export const FITS: Schemas["LabelValue"][] = ["central", "related", "adjacent", "none", "unsure"];

export function Seg<T extends string>({ values, value, auto, onPick, disabled, clear, label }: { values: readonly T[]; value: T | null | undefined; auto?: T | null; onPick: (v: T | null) => void; disabled?: boolean; clear?: boolean; label: string }) {
  return (
    <div className="fitseg" role="group" aria-label={label}>
      {values.map((v) => (
        <button key={v} className={`seg${value === v ? " on" : ""}${!value && auto === v ? " auto" : ""}`} aria-pressed={value === v} disabled={disabled} onClick={() => onPick(clear && value === v ? null : v)}>
          {v.replace(/_/g, " ")}
        </button>
      ))}
    </div>
  );
}
