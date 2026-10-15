import { render, screen } from "@testing-library/react";
import { App } from "./App";

test("the placeholder says the web app is being rebuilt", () => {
  render(<App />);
  expect(screen.getByText("The v1 web app is being rebuilt.")).toBeInTheDocument();
});
