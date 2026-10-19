import { Link } from "@tanstack/react-router";
import type { ReactNode } from "react";

// A paper a digest's text names is marked [the name it uses](its arXiv id) (digests.md, writing).
const MARK = /\[([^[\]]+)\]\(([^()\s]+)\)/g;

/** Generated prose (a Brief, a digest's text): a paragraph for each block between blank lines. A marked paper shows
 * as its name, linking to its page, opened under `topic` where one is given. */
export function Prose({ text, topic }: { text: string; topic?: string }) {
  return (
    <>
      {text
        .split(/\n\s*\n/)
        .filter((p) => p.trim())
        .map((p, i) => (
          <p key={i} className="prose">
            {linked(p, topic)}
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
