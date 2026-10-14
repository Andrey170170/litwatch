import "@testing-library/jest-dom/vitest";
import { configure } from "@testing-library/react";

// A first render in a file can pass the default 1 s on a slow CI runner.
configure({ asyncUtilTimeout: 5000 });

// jsdom has no matchMedia; the layout hook asks for the desktop breakpoint.
if (!window.matchMedia) {
  window.matchMedia = (query: string) =>
    ({
      matches: query.includes("min-width") ? true : false,
      media: query,
      onchange: null,
      addEventListener: () => {},
      removeEventListener: () => {},
      addListener: () => {},
      removeListener: () => {},
      dispatchEvent: () => false,
    }) as MediaQueryList;
}
