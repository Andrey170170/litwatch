/**
 * The Deep read's contents (docs/specs/deep-read.md, "On the paper page"): one entry per section with its colour,
 * the section in view lit, a click scrolling to it. In the rail on the desktop (`Contents`), a sticky strip of chips
 * under the top bar on the phone (`Chips`). Both watch the read's section rungs by their ids.
 */
import { useEffect, useRef, useState } from "react";
import { sectionMark, type Section } from "./types";

/** A section rung's element id, by the section's own. */
export const sectionId = (id: string) => `dr-${id}`;

/** The nearest ancestor that scrolls (the app's `.main`, the phone's `.scroll`), or null for the page itself. */
function scrollerOf(el: Element): HTMLElement | null {
  for (let p = el.parentElement; p; p = p.parentElement) {
    if (/(auto|scroll)/.test(getComputedStyle(p).overflowY)) return p;
  }
  return null;
}

/** Which section of the read is being read, and whether the read is in view at all. The section being read is the
 * last one whose top has passed the line a section lands on when its contents entry is clicked (its scroll margin
 * under the scroller's top); between sections the last one stays. At the end of the scroll the last section is
 * the one being read, if it is in sight, since it may never reach that line. A click's section stays lit until the
 * reader scrolls on, even when the scroll could not bring it to the line. */
export function useReadSpy(sections: Section[]): { current: string | null; inView: boolean; go: (id: string) => void } {
  const [current, setCurrent] = useState<string | null>(null);
  const [inView, setInView] = useState(false);
  // where the scroller stood after a click, and so whether the reader has scrolled since
  const pinned = useRef<number | null>(null);
  const position = useRef<() => number>(() => 0);
  const ids = sections.map((s) => s.id).join(" ");
  useEffect(() => {
    const els = ids
      .split(" ")
      .map((id) => document.getElementById(sectionId(id)))
      .filter((el): el is HTMLElement => el !== null);
    const read = els[0]?.closest(".dread");
    if (!read) return;
    const box = scrollerOf(read);
    const pos = () => (box ? box.scrollTop : window.scrollY);
    position.current = pos;
    const pick = () => {
      if (pinned.current !== null) {
        if (Math.abs(pos() - pinned.current) < 4) return;
        pinned.current = null;
      }
      const view = box ? box.getBoundingClientRect() : { top: 0, bottom: window.innerHeight };
      const end = box ? box.scrollHeight - box.clientHeight : document.documentElement.scrollHeight - window.innerHeight;
      const last = els[els.length - 1]!;
      if (pos() > 0 && pos() >= end - 2 && last.getBoundingClientRect().top < view.bottom) {
        setCurrent(last.dataset.section!);
        return;
      }
      const line = view.top + (parseFloat(getComputedStyle(els[0]!).scrollMarginTop) || 0) + 8;
      const passed = els.findLast((el) => el.getBoundingClientRect().top <= line);
      if (passed) setCurrent(passed.dataset.section!);
    };
    const target = box ?? window;
    target.addEventListener("scroll", pick, { passive: true });
    window.addEventListener("resize", pick);
    pick();
    let view: IntersectionObserver | undefined;
    if (typeof IntersectionObserver !== "undefined") {
      view = new IntersectionObserver(([e]) => e && setInView(e.isIntersecting), { rootMargin: "-40% 0px -40% 0px" });
      view.observe(read);
    }
    return () => {
      target.removeEventListener("scroll", pick);
      window.removeEventListener("resize", pick);
      view?.disconnect();
    };
  }, [ids]);
  // the focus goes with the scroll, so the next Tab is in the section and not back in the contents
  const go = (id: string) => {
    const el = document.getElementById(sectionId(id));
    el?.scrollIntoView({ block: "start" });
    el?.focus({ preventScroll: true });
    pinned.current = position.current();
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
