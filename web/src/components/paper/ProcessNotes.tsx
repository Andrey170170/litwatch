import { useState } from "react";
import type { ProcessNote } from "@/lib/settings";
import { Rung } from "./Rung";

/** A URL to link, only when it is http(s): a note's URL comes from the model, and is never a `javascript:` link. */
export function webUrl(url: string | undefined): URL | null {
  if (!url) return null;
  try {
    const u = new URL(url);
    return u.protocol === "https:" || u.protocol === "http:" ? u : null;
  } catch {
    return null;
  }
}

/** What Deep's own process held back (GLOSSARY.md, process note): folded under "Process notes · N" until opened, each
 * note with its kind as a tag and its page as a link. It starts folded on every card and every read. */
export function ProcessNotes({ notes }: { notes: ProcessNote[] }) {
  const [open, setOpen] = useState(false);
  return (
    <Rung name="" className="sub pnotes">
      <button className="tpfold" onClick={() => setOpen(!open)} aria-expanded={open}>
        <span className={`car${open ? " open" : ""}`} aria-hidden />
        <span className="ct">
          Process notes · <b>{notes.length}</b>
        </span>
      </button>
      {open && (
        <ul className="lim">
          {notes.map((n, i) => {
            const link = webUrl(n.url);
            return (
              <li key={i}>
                <i className="tag">{n.kind}</i>
                {n.note}
                {link ? (
                  <>
                    {" "}
                    <a className="lnk" href={link.href} target="_blank" rel="noopener noreferrer" title={link.href}>
                      {link.hostname} ↗
                    </a>
                  </>
                ) : n.url ? (
                  <span className="faint"> {n.url}</span>
                ) : null}
              </li>
            );
          })}
        </ul>
      )}
    </Rung>
  );
}
