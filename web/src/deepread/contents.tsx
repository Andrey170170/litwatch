/**
 * The Deep read's contents (docs/specs/deep-read.md, "On the paper page"): one entry per section with its colour,
 * the section in view lit, a click scrolling to it. In the rail on the desktop (`Contents`), a sticky strip of chips
 * under the top bar on the phone (`Chips`). Both watch the read's section rungs by their ids.
 */
import { useEffect, useRef, useState } from "react";
import { sectionMark, type Section } from "./types";

/** A section rung's element id, by the section's own. */
export const sectionId = (id: string) => `dr-${id}`;

/** Which section of the read is being read, and whether the read is in view at all. A section is current while it
 * crosses a band a third of the way down the view; where two do (one ends, the next begins), the later one is.
 * Between sections the last one stays. */
export function useReadSpy(sections: Section[]): { current: string | null; inView: boolean; go: (id: string) => void } {
  const [current, setCurrent] = useState<string | null>(null);
  const [inView, setInView] = useState(false);
  const ids = sections.map((s) => s.id).join(" ");
  useEffect(() => {
    if (typeof IntersectionObserver === "undefined") return;
    const order = ids.split(" ");
    const els = order.map((id) => document.getElementById(sectionId(id))).filter((el): el is HTMLElement => el !== null);
    const read = els[0]?.closest(".dread");
    if (!read) return;
    const crossing = new Set<string>();
    const spy = new IntersectionObserver(
      (entries) => {
        for (const e of entries) {
          const id = (e.target as HTMLElement).dataset.section!;
          if (e.isIntersecting) crossing.add(id);
          else crossing.delete(id);
        }
        const last = order.findLast((id) => crossing.has(id));
        if (last) setCurrent(last);
      },
      { rootMargin: "-30% 0px -60% 0px" },
    );
    els.forEach((el) => spy.observe(el));
    const view = new IntersectionObserver(([e]) => e && setInView(e.isIntersecting), { rootMargin: "-40% 0px -40% 0px" });
    view.observe(read);
    return () => {
      spy.disconnect();
      view.disconnect();
    };
  }, [ids]);
  const go = (id: string) => {
    document.getElementById(sectionId(id))?.scrollIntoView({ block: "start" });
    setCurrent(id);
  };
  return { current, inView, go };
}

type Props = { sections: Section[]; topicColour: (slug: string) => string | undefined };

/** The rail's contents: dimmed while the read is out of view. */
export function Contents({ sections, topicColour }: Props) {
  const { current, inView, go } = useReadSpy(sections);
  return (
    <nav className={`dr-toc${inView ? "" : " away"}`} aria-label="Deep read">
      <div className="rh">Deep read</div>
      {sections.map((s) => (
        <button key={s.id} className={s.id === current ? "cur" : undefined} aria-current={s.id === current ? "location" : undefined} onClick={() => go(s.id)} {...sectionMark(s, topicColour)}>
          <span className="sw" aria-hidden />
          {s.title}
        </button>
      ))}
    </nav>
  );
}

/** The phone's strip of section chips: the current one outlined and scrolled into sight. It is the first thing in
 * the read and sticks under the top bar for as long as the read is on screen. */
export function Chips({ sections, topicColour }: Props) {
  const { current, go } = useReadSpy(sections);
  const strip = useRef<HTMLElement>(null);
  useEffect(() => {
    const el = strip.current;
    const chip = el?.querySelector<HTMLElement>(".cur");
    if (el && chip) el.scrollTo({ left: chip.offsetLeft - 16, behavior: "smooth" });
  }, [current]);
  return (
    <nav className="dr-chips" ref={strip} aria-label="Deep read">
      {sections.map((s) => (
        <button key={s.id} className={s.id === current ? "cur" : undefined} aria-current={s.id === current ? "location" : undefined} onClick={() => go(s.id)} {...sectionMark(s, topicColour)}>
          <span className="c">
            <span className="sw" aria-hidden />
            {s.title}
          </span>
        </button>
      ))}
    </nav>
  );
}
