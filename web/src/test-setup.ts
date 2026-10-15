import "@testing-library/jest-dom/vitest";

// jsdom has no element scrolling; the router scrolls the main column to the top on navigation.
Element.prototype.scrollTo = () => {};
