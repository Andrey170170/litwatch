import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { Sheet } from "./Sheet";

const finger = (x: number, y: number, pointerType = "touch") => ({ clientX: x, clientY: y, pointerId: 1, pointerType, isPrimary: true, button: 0, buttons: 1 });

describe("A sheet's body drag", () => {
  it("leaves a finger on a list scrolled down inside it to that list, and takes one at the list's top", () => {
    const onClose = vi.fn();
    render(
      <Sheet open onClose={onClose} label="Go to a paper">
        <div data-testid="list">
          <p>Rows</p>
        </div>
      </Sheet>,
    );
    const sheet = screen.getByRole("dialog");
    const list = screen.getByTestId("list");
    Object.defineProperty(list, "scrollTop", { value: 40, configurable: true });
    fireEvent.pointerDown(list, finger(100, 100));
    fireEvent.pointerMove(window, finger(100, 220));
    expect(sheet.style.transform).toBe("");
    fireEvent.pointerUp(window, finger(100, 220));
    expect(onClose).not.toHaveBeenCalled();

    Object.defineProperty(list, "scrollTop", { value: 0, configurable: true });
    fireEvent.pointerDown(list, finger(100, 100));
    fireEvent.pointerMove(window, finger(100, 220));
    expect(sheet.style.transform).toBe("translateY(120px)");
  });

  it("leaves a text field its pointer, and a pen on the body to the browser, while the handle takes either", () => {
    render(
      <Sheet open onClose={vi.fn()} label="Add a paper">
        <input aria-label="Paste a DOI" />
      </Sheet>,
    );
    const sheet = screen.getByRole("dialog");
    const drag = (from: Element, pointerType: string) => {
      fireEvent.pointerDown(from, finger(100, 100, pointerType));
      fireEvent.pointerMove(window, finger(100, 220, pointerType));
      const moved = sheet.style.transform;
      fireEvent.pointerUp(window, finger(100, 220, pointerType));
      sheet.style.transform = "";
      return moved;
    };
    expect(drag(screen.getByRole("textbox"), "touch")).toBe("");
    expect(drag(sheet, "pen")).toBe("");
    expect(drag(sheet.querySelector(".hdl")!, "pen")).toBe("translateY(120px)");
  });
});
