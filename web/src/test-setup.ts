import "@testing-library/jest-dom/vitest";

// jsdom has no element scrolling; the router scrolls the main column to the top on navigation.
Element.prototype.scrollTo = () => {};
import { afterEach } from "vitest";
import { clearToasts } from "@/lib/toast";

// Toasts are a module-level store and outlive a test's render: start each case without the last one's.
afterEach(() => clearToasts());
