/**
 * The render page (docs/plans/deep-read.md, D1): `/render.html`, built beside the app and served by litwatch like
 * its other static files. The deep service's browser opens it and hands it a draft, so the agent sees a read drawn
 * by the components the owner's page uses. The page holds no data, takes no token and makes no request of the API:
 * everything it draws is passed in.
 *
 * How to drive it:
 *
 *   await page.goto(`${LITWATCH_URL}/render.html`);
 *   await page.evaluate(([read, opts]) => window.renderRead(read, opts), [read, { figures }]);
 *
 * `renderRead(read, options)` draws the read and resolves once it is drawn: fonts loaded, figures decoded. It may
 * be called again with another draft. `options`:
 *   - `figures`: `{ [figure name]: data URL }`. A figure the read names and this leaves out shows its placeholder.
 *   - `topics`: `{ [topic slug]: CSS colour }`, the colour of each lens; a lens left out is neutral.
 *   - `theme`: "light" or "dark"; left out, the page follows the browser's colour scheme.
 *
 * The state is on `<html data-render>`: absent until the first call, then "drawing", then "ready", or "error"
 * with the message in `data-render-error` (and the promise rejects): the draft is not a read, or its shape is so
 * far from the schema that drawing it failed. The page does not validate a draft; that is the deep service's.
 *
 * What a screenshot can target: a section is `#dr-<section id>` (also `[data-section="<id>"]`), a block is
 * `[data-block="<section id>.<index>"]`, its index counted from 0 within the section; the whole read is `.dread`.
 * The layout follows the viewport as the app's does: the phone's below 896px wide, with the strip of section chips.
 */
import { StrictMode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { flushSync } from "react-dom";
import "../styles.css";
import "./deepread.css";
import { DeepRead } from "./DeepRead";
import type { ReadEnv } from "./env";
import { isRead, type Read } from "./types";

export type RenderOptions = { figures?: Record<string, string>; topics?: Record<string, string>; theme?: "light" | "dark" };

declare global {
  interface Window {
    renderRead: (read: unknown, options?: RenderOptions) => Promise<void>;
  }
}

// No page to go to from here: the related papers are drawn as on the paper page and lead nowhere.
const PaperLink: ReadEnv["PaperLink"] = ({ className, style, children }) => (
  <span className={className} style={style}>
    {children}
  </span>
);

function Page({ read, options }: { read: Read; options: RenderOptions }) {
  const env: ReadEnv = {
    figureSrc: (name) => (options.figures && Object.hasOwn(options.figures, name) ? options.figures[name] : undefined),
    topicColour: (slug) => (options.topics && Object.hasOwn(options.topics, slug) ? options.topics[slug] : undefined),
    PaperLink,
    eager: true,
  };
  // the collection's papers are drawn from the read itself: the page knows no more of them
  const among = read.among_collection.map((a) => ({ id: a.arxiv_id, title: a.title, relation: a.relation, note: a.note, rarity: null, in_collection: true }));
  return (
    <div className="pg dr-render">
      <div className="ladder">
        <DeepRead read={read} among={among} env={env} />
      </div>
    </div>
  );
}

let root: Root | null = null;
// what React caught while drawing: it reports a failed render here instead of throwing it
let failure: { error: unknown } | null = null;

window.renderRead = async (read, options = {}) => {
  const html = document.documentElement;
  html.dataset.render = "drawing";
  delete html.dataset.renderError;
  try {
    if (!isRead(read)) throw new Error('not a Deep read: its format is not "read"');
    if (options.theme) html.dataset.theme = options.theme;
    else delete html.dataset.theme;
    root ??= createRoot(document.getElementById("root")!, { onUncaughtError: (error) => (failure = { error }) });
    failure = null;
    flushSync(() =>
      root!.render(
        <StrictMode>
          <Page read={read} options={options} />
        </StrictMode>,
      ),
    );
    if (failure) throw (failure as { error: unknown }).error;
    await document.fonts?.ready;
    // a figure that cannot be decoded shows its placeholder, which is drawn already
    await Promise.all([...document.images].map((img) => img.decode?.().catch(() => {})));
    html.dataset.render = "ready";
  } catch (e) {
    html.dataset.render = "error";
    html.dataset.renderError = e instanceof Error ? e.message : String(e);
    throw e;
  }
};
