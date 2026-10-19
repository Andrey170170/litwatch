import { Link } from "@tanstack/react-router";
import type { ReactNode } from "react";

// A paper a digest's text names is marked [the words it uses](its arXiv id), as `core.digests.MARK` reads it
// (digests.md, writing): the words may hold one level of brackets, the id is a new-style or an old-style one.
const MARK = /\[((?:[^[\]]|\[[^[\]]*\])+)\]\((\d{4}\.\d{4,5}(?:v\d+)?|[a-z-]+(?:\.[A-Z]{2})?\/\d{7}(?:v\d+)?)\)/g;

/** Generated prose (a Brief, a digest's text): a paragraph for each block between blank lines. With `marks` (a
 * digest's text), a marked paper shows as its words, linking to its page, opened under `topic` where one is given;
 * other text, a Brief's included, shows as written. */
export function Prose({ text, marks, topic }: { text: string; marks?: boolean; topic?: string }) {
  return (
    <>
      {text
        .split(/\n\s*\n/)
        .filter((p) => p.trim())
        .map((p, i) => (
          <p key={i} className="prose">
            {marks ? linked(p, topic) : p}
          </p>
        ))}
    </>
  );
}

function linked(text: string, topic?: string): ReactNode[] {
  const out: ReactNode[] = [];
  let at = 0;
  for (const m of text.matchAll(MARK)) {
    out.push(text.slice(at, m.index));
    out.push(
      <Link key={m.index} to="/paper/$" params={{ _splat: m[2] }} search={topic ? { topic } : {}} className="pl">
        {m[1]}
      </Link>,
    );
    at = m.index + m[0].length;
  }
  out.push(text.slice(at));
  return out;
}
