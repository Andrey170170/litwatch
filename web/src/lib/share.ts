/**
 * The share target (DESIGN.md, "Phone"; feedback.md, "Misses"): Android hands the app a link, a text and a title.
 * An arXiv id found in any of them lands on the paper with "Report as a miss" open; anything else is searched.
 */

// A new-style id (2401.12345, with or without a version), then an old one (hep-th/9901001, math.GT/0309136): one
// of arXiv's archives, so that any path with seven digits (example.org/reports/1234567) is not a paper.
const NEW = /(?<!\d)(\d{4}\.\d{4,5})(?:v\d+)?(?!\d)/;
const ARCHIVES =
  "astro-ph|cond-mat|gr-qc|hep-ex|hep-lat|hep-ph|hep-th|math-ph|nlin|nucl-ex|nucl-th|physics|quant-ph|math|cs|q-bio|q-fin|stat|eess|econ" +
  // the archives arXiv has since folded into those, whose ids still resolve
  "|acc-phys|adap-org|alg-geom|ao-sci|atom-ph|bayes-an|chao-dyn|chem-ph|cmp-lg|comp-gas|dg-ga|funct-an|mtrl-th|patt-sol|plasm-ph|q-alg|solv-int|supr-con";
const OLD = new RegExp(`(?<![\\w-])((?:${ARCHIVES})(?:\\.[A-Z]{2})?\\/\\d{7})(?:v\\d+)?(?!\\d)`);

/** The arXiv id in the texts given, the first text with one deciding (the link before the text that mentions
 * it); null when none. */
export function arxivIdIn(...texts: (string | null | undefined)[]): string | null {
  for (const t of texts) for (const re of [NEW, OLD]) {
      const m = t && re.exec(t);
      if (m) return m[1]!;
    }
  return null;
}

export type Shared = { url?: string; text?: string; title?: string };
export type Landing = { kind: "paper"; id: string } | { kind: "search"; q: string };

/** Where a share lands: the paper named by the id found, else Papers searching the text shared. */
export function landingFor(s: Shared): Landing {
  const id = arxivIdIn(s.url, s.text, s.title);
  if (id) return { kind: "paper", id };
  const q = [s.title, s.text, s.url].find((t) => t?.trim()) ?? "";
  return { kind: "search", q: q.trim() };
}
