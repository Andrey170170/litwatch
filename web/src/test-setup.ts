import "@testing-library/jest-dom/vitest";

// jsdom has no element scrolling; the router scrolls the main column to the top on navigation.
Element.prototype.scrollTo = () => {};
import { afterEach } from "vitest";
import { forgetAllCards } from "@/lib/remember";
import { clearToasts } from "@/lib/toast";

// Toasts and the cards asked for are module-level stores and outlive a test's render: start each case without the
// last one's.
afterEach(() => {
  clearToasts();
  forgetAllCards();
  delete (globalThis as { __renders?: unknown }).__renders;
});
