import katex from "katex";
import "katex/dist/katex.min.css";
import { useMemo } from "react";

/** TeX drawn by KaTeX, inline or as a display equation. TeX that does not parse shows as KaTeX's own error text in
 * place (the deep service rejects it at submit, so a stored read's equations parse). `trust` stays off: no links or
 * images from TeX. */
export function Tex({ tex, display }: { tex: string; display?: boolean }) {
  const html = useMemo(() => katex.renderToString(tex, { displayMode: !!display, throwOnError: false }), [tex, display]);
  return <span className={display ? "dr-tex disp" : "dr-tex"} dangerouslySetInnerHTML={{ __html: html }} />;
}
