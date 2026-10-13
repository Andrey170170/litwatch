/**
 * What the owner pasted as a published version: a DOI (bare, `doi:`-prefixed, or a doi.org
 * link) or else a DBLP key (bare or as a dblp.org/rec link). SPEC §6.
 */
export type PublicationBody = { doi: string } | { dblp_key: string };

export function publicationBody(input: string): PublicationBody | null {
  let v = input.trim();
  if (!v) return null;
  v = v.replace(/^doi:\s*/i, "").replace(/^https?:\/\/(dx\.)?doi\.org\//i, "");
  if (/^10\.\d+\//.test(v)) return { doi: v };
  v = v.replace(/^https?:\/\/dblp\.org\/rec\//i, "").replace(/\.html$/i, "");
  return { dblp_key: v };
}
