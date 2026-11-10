import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useReadEnv } from "./env";
import { Inline } from "./inline";
import type { BlockOf } from "./types";

/** One of the paper's own figures, with its number in the paper and the read's caption; a click opens it large,
 * and a click anywhere or Escape closes it, the focus going in, staying in and going back out with it. The image comes from the page
 * (`ReadEnv.figureSrc`); when there is none, or it does not load, the caption stands under a quiet placeholder
 * that says which. */
export function Figure({ block }: { block: BlockOf<"fig"> }) {
  const env = useReadEnv();
  const src = env.figureSrc(block.name);
  // the address that failed, so another one (a read written again) is tried afresh
  const [failed, setFailed] = useState<string | null>(null);
  const [zoom, setZoom] = useState(false);
  const opener = useRef<HTMLButtonElement>(null);
  const close = () => {
    setZoom(false);
    opener.current?.focus();
  };
  useEffect(() => {
    if (!zoom) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") close();
      // the close button is all there is in it, so the focus stays on it
      else if (e.key === "Tab") e.preventDefault();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [zoom]);
  const shown = src && failed !== src;
  return (
    <figure className="dr-fig">
      {shown ? (
        <button ref={opener} className="fi" onClick={() => setZoom(true)} aria-label={`${block.number}, larger`}>
          <img src={src} alt={block.number} loading={env.eager ? "eager" : "lazy"} onError={() => {
            // a zoom open on it closes too, taking its hold on the keyboard with it
            setFailed(src);
            setZoom(false);
          }} />
        </button>
      ) : (
        <div className="fi nofig">
          {block.number} · {src ? "the image did not load" : "the image is not here"}
        </div>
      )}
      <figcaption>
        <b>{block.number}</b> · <Inline text={block.caption} />
      </figcaption>
      {/* on the body: the page's column is a container, which would hold a fixed layer inside itself */}
      {zoom &&
        shown &&
        createPortal(
          <div className="dr-zoom" role="dialog" aria-modal="true" aria-label={block.number} onClick={close}>
            <button autoFocus aria-label="Close the figure">
              <img src={src} alt={block.number} />
            </button>
          </div>,
          document.body,
        )}
    </figure>
  );
}
