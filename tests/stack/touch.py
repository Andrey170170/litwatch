"""The phone's gestures under real touch (DESIGN.md, "Phone"): a finger drawn through Chromium's
compositor (CDP `Input.dispatchTouchEvent`), so `touch-action`, the browser's own pans and the
`pointercancel` they send behave as on a phone, which a synthetic PointerEvent never shows. Each
gesture must do what the design says, and a vertical finger must still scroll natively.

    python touch.py BASE_URL TOKEN OUT

Needs `playwright` and its Chromium. A gesture whose data the stack lacks (an older day, a
Calibrate session) is skipped and said so."""

from __future__ import annotations

import sys
import time
from datetime import date
from pathlib import Path

from playwright.sync_api import sync_playwright

PHONE = {"width": 390, "height": 844}
PAPER_LINK = 'main a[href^="/paper/"]'


class Finger:
    """One finger on the glass, moved through the compositor."""

    def __init__(self, page) -> None:
        self.cdp = page.context.new_cdp_session(page)

    def drag(self, x0: float, y0: float, x1: float, y1: float, steps: int = 12) -> None:
        self.touch("touchStart", x0, y0)
        for i in range(1, steps + 1):
            time.sleep(0.016)
            self.touch("touchMove", x0 + (x1 - x0) * i / steps, y0 + (y1 - y0) * i / steps)
        time.sleep(0.016)
        self.cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})

    def touch(self, kind: str, x: float, y: float) -> None:
        self.cdp.send("Input.dispatchTouchEvent", {"type": kind, "touchPoints": [{"x": x, "y": y}]})


def settle(page, ms: int = 700) -> None:
    page.wait_for_timeout(ms)


def unfold(page) -> None:
    """Open a list's folded shelves that hold papers: a fresh stack's papers are common, in the
    shelf folded by default. A head shows its count once its shelf has loaded; an empty one is
    still loading, and a head whose shelf proves empty goes."""
    page.locator("main .shh .k:not(:empty)").first.wait_for()
    folded = page.locator("main button.shh.fold:has(.k:not(:empty))")
    while folded.count():
        folded.first.click()
        page.wait_for_load_state("networkidle")
    # an opened shelf shows the one row it had while folded, then its page: wait for the rows to
    # stop coming
    links = page.locator(PAPER_LINK)
    links.first.wait_for()
    for _ in range(20):
        before = links.count()
        page.wait_for_timeout(500)
        if links.count() == before:
            break


def gestures(base: str, token: str, out: Path) -> list[str]:
    """Every gesture, each a line when it fails or is skipped."""
    lines: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(viewport=PHONE, has_touch=True, is_mobile=True)
        assert context.request.post(f"{base}/api/session", data={"token": token}).ok, "signing in"
        page = context.new_page()
        finger = Finger(page)
        shot = lambda name: page.screenshot(path=out / f"touch-{name}.png")  # noqa: E731

        def check(name: str, ok: bool, detail: str = "") -> bool:
            shot(name)
            if not ok:
                lines.append(f"FAIL {name}{': ' + detail if detail else ''}")
            return ok

        # the drawer, by a swipe from the edge (inside Android's gesture strip); shut by a swipe
        # from the shade
        page.goto(f"{base}/", wait_until="networkidle")
        finger.drag(36, 400, 230, 400)
        settle(page)
        if check("drawer-opens", page.locator('.drawer[data-state="open"]').count() == 1):
            finger.drag(350, 400, 110, 400)
            settle(page)
            check("drawer-shuts-from-shade", page.locator(".drawer").count() == 0)

        # the Day, stepped to the older one: a swipe to the right, past the edge zone
        today = date.today().isoformat()
        day = context.request.get(f"{base}/api/digests/days/{today}")
        previous = day.json().get("previous") if day.ok else None
        if previous:
            page.goto(f"{base}/", wait_until="networkidle")
            finger.drag(100, 420, 340, 420)
            settle(page)
            check("day-steps-older", f"day={previous}" in page.url, page.url)
        else:
            lines.append("SKIP day-steps-older: no older day")

        # a paper, stepped to the next in its list; then the edge swipe back, which returns to the
        # first paper, as a step is a history entry
        page.goto(f"{base}/papers", wait_until="networkidle")
        unfold(page)
        links = page.locator(PAPER_LINK)
        hrefs = [a.get_attribute("href") or "" for a in links.all()]
        if len(hrefs) >= 2:
            first = hrefs[0]
            links.nth(0).click()
            page.wait_for_url("**/paper/**")
            settle(page, 500)
            finger.drag(300, 420, 60, 420)
            settle(page)
            stepped = "/paper/" in page.url and not page.url.endswith(first)
            if check("paper-steps-next", stepped, page.url):
                finger.drag(36, 420, 330, 420)
                settle(page)
                check("edge-swipe-goes-back", page.url.endswith(first), page.url)
        else:
            # the stack seeds its papers: fewer than two listed is the list's failure, not a skip
            lines.append(f"FAIL paper-steps-next, edge-swipe-goes-back: {len(hrefs)} papers listed")

        # a Calibrate session's cards
        sessions = context.request.get(f"{base}/api/settings/calibrate")
        rows = sessions.json() if sessions.ok else []
        if rows and rows[0]["papers"] >= 2:
            page.goto(f"{base}/settings/calibrate/{rows[0]['id']}?at=1", wait_until="networkidle")
            finger.drag(300, 500, 60, 500)
            settle(page)
            check("calibrate-steps-next", "at=2" in page.url, page.url)
        else:
            lines.append("SKIP calibrate-steps-next: no session of two papers")

        # a sheet, dragged down by its body from the top
        page.goto(f"{base}/papers", wait_until="networkidle")
        page.get_by_role("button", name="Filters").click()
        sheet = page.locator(".sheet")
        sheet.wait_for()
        settle(page, 400)
        box = sheet.bounding_box()
        assert box, "the sheet has no box"
        finger.drag(195, box["y"] + 70, 195, box["y"] + 330)
        settle(page)
        check("sheet-body-drags-shut", page.locator(".sheet").count() == 0)

        # a vertical finger scrolls the page as the browser does: a paper's page, which is long
        if hrefs:
            page.goto(f"{base}{hrefs[0]}", wait_until="networkidle")
            screen = "document.querySelector('.phone .scroll')"
            if page.evaluate(f"{screen}.scrollHeight > {screen}.clientHeight"):
                finger.drag(200, 700, 200, 250)
                settle(page, 400)
                scrolled = page.evaluate(f"{screen}.scrollTop")
                check("vertical-scrolls", scrolled > 0, f"scrollTop {scrolled}")
            else:
                lines.append("SKIP vertical-scrolls: the paper's page does not overflow")
        else:
            lines.append("FAIL vertical-scrolls: no paper listed")

        browser.close()
    return lines


def main(base: str, token: str, out: str) -> None:
    shots = Path(out)
    shots.mkdir(parents=True, exist_ok=True)
    lines = gestures(base, token, shots)
    for line in lines:
        print(line)
    failures = [line for line in lines if line.startswith("FAIL")]
    print(f"{len(failures)} failures; screenshots in {shots}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main(*sys.argv[1:4])
