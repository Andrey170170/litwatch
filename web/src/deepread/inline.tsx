import type { ReactNode } from "react";
import { Tex } from "./math";

// One alternative per mark, tried where the text stands: an escaped mark, `code`, $tex$, **bold**, *italic*,
// [words](url). Maths follows pandoc's rule, so an amount of money is not an equation: the opening $ has no space
// after it, the closing one none before it and no digit after it.
const MARK =
  /\\([\\*`$[\]])|`([^`\n]+)`|\$(?!\s)((?:\\.|[^$\\])+?)(?<!\s)\$(?!\d)|\*\*(?!\s)(.+?)(?<!\s)\*\*|\*(?![\s*])(.+?)(?<![\s*])\*|\[([^[\]\n]+)\]\((https?:\/\/[^\s()]+)\)/g;

/** The read's inline text (`deep/src/read.ts`): **bold**, *italic*, `code`, $tex$ and [words](https://…), and
 * nothing else. The text is the model's, so anything else, HTML included, shows as written, and a link is made
 * only for an http(s) address. */
export function inline(text: string): ReactNode[] {
  const out: ReactNode[] = [];
  let at = 0;
  for (const m of text.matchAll(MARK)) {
    if (m.index > at) out.push(text.slice(at, m.index));
    const key = m.index;
    if (m[1] !== undefined) out.push(m[1]);
    else if (m[2] !== undefined) out.push(<code key={key}>{m[2]}</code>);
    else if (m[3] !== undefined) out.push(<Tex key={key} tex={m[3]} />);
    else if (m[4] !== undefined) out.push(<b key={key}>{inline(m[4])}</b>);
    else if (m[5] !== undefined) out.push(<i key={key}>{inline(m[5])}</i>);
    else
      out.push(
        <a key={key} className="dr-a" href={m[7]} target="_blank" rel="noopener noreferrer">
          {inline(m[6]!)}
        </a>,
      );
    at = m.index + m[0].length;
  }
  if (at < text.length) out.push(text.slice(at));
  return out;
}

export const Inline = ({ text }: { text: string }) => <>{inline(text)}</>;
