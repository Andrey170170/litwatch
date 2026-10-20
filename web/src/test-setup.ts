import "@testing-library/jest-dom/vitest";

// jsdom has no element scrolling; the router scrolls the main column to the top on navigation.
Element.prototype.scrollTo = () => {};
import { afterEach } from "vitest";
import { forgetAllCards, forgetShelves } from "@/lib/remember";
import { clearToasts } from "@/lib/toast";

// Toasts, the cards asked for and the folds toggled are module-level stores and outlive a test's render: start each
// case without the last one's.
afterEach(() => {
  clearToasts();
  forgetAllCards();
  forgetShelves();
  delete (globalThis as { __renders?: unknown }).__renders;
});
