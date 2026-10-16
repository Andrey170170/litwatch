"""The phone's gestures under real touch (DESIGN.md, "Phone"): a finger drawn through Chromium's
compositor (CDP `Input.dispatchTouchEvent`), so `touch-action`, the browser's own pans and the
`pointercancel` they send behave as on a phone, which a synthetic PointerEvent never shows. Each
gesture must do what the design says, and a vertical finger must still scroll natively.

    python touch.py BASE_URL TOKEN OUT [DAY]

DAY is the Day the walk steps from, the stack's own day (its papers' day, as smoke.sh knows it);
without it, the runner's today, which the stack's clock and the browser's may not share. Needs
`playwright` and its Chromium. A gesture whose data the stack lacks (an older day, a Calibrate
session) is skipped and said so."""

from __future__ import annotations

import sys
import time
from datetime import date
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).parent))
from pages import watch  # noqa: E402

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


def arrived(page, where: str, ms: int = 5000) -> bool:
    """Whether the page reaches `where` within `ms` and shows it: a path, which a paper's page
    shows by its id, or a query parameter (`at=2`) held exactly."""
    path = urlparse(where).path
    try:
        page.wait_for_url(lambda url: reaches(url, where), timeout=ms)
        if path.startswith("/paper/"):
            page.locator("main", has_text=path.rsplit("/", 1)[1]).first.wait_for(timeout=ms)
    except PlaywrightTimeoutError:
        return False
    settle(page, 300)
    return True


def reaches(url: str, where: str) -> bool:
    """Whether `url` is at `where`: a path it ends with, or a query parameter it holds exactly."""
    parts = urlparse(url)
    if "=" in where and "/" not in where:
        name, value = where.split("=", 1)
        return parse_qs(parts.query).get(name) == [value]
    return parts.path.endswith(urlparse(where).path)


def shelf_links(section) -> list[str]:
    """The papers a shelf lists, by href."""
    return [a.get_attribute("href") or "" for a in section.locator('a[href^="/paper/"]').all()]


def pair_of(page) -> list[str]:
    """Two papers of one shelf, the step's list: from a shelf already open, else from the folded
    shelves opened in turn, as a fresh stack's papers are common, in the shelf folded by default;
    none when no shelf lists two. A head shows its count once its shelf has loaded, so an empty
    head is still loading, and a head whose shelf proves empty goes. An opened shelf shows the one
    row it had while folded, then its page: its rows are given time to come."""
    page.locator("main .shh .k:not(:empty)").first.wait_for()
    while True:
        for section in page.locator("main section").all():
            if len(links := shelf_links(section)) >= 2:
                return links
        folded = page.locator("main button.shh.fold:has(.k:not(:empty))").first
        if not folded.count():
            return []
        # the shelf by its name: the head loses its `fold` class as it opens
        name = folded.locator("xpath=ancestor::section[1]").get_attribute("aria-label")
        section = page.locator(f'main section[aria-label="{name}"]')
        folded.click()
        for _ in range(20):
            page.wait_for_timeout(500)
            if len(shelf_links(section)) >= 2:
                break


def gestures(base: str, token: str, out: Path, day: str) -> list[str]:
    """Every gesture, each a line when it fails or is skipped. A check that cannot go on raises,
    with what the browser saw by then noted on the error."""
    lines: list[str] = []
    errors: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            context = browser.new_context(viewport=PHONE, has_touch=True, is_mobile=True)
            signed = context.request.post(f"{base}/api/session", data={"token": token})
            assert signed.ok, f"signing in: HTTP {signed.status}"
            page = context.new_page()
            errors = watch(page)
            walk(page, base, out, day, lines)
        except Exception as e:
            for seen in errors:
                e.add_note(f"the browser saw: {seen}")
            raise
        finally:
            browser.close()
    return lines + [f"FAIL the browser saw: {e}" for e in errors]


def walk(page, base: str, out: Path, day: str, lines: list[str]) -> None:
    """The gestures in turn, each adding its line to `lines` when it fails or is skipped."""
    context = page.context
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
    read = context.request.get(f"{base}/api/digests/days/{day}")
    assert read.ok, f"reading the Day {day}: HTTP {read.status}"
    previous = read.json().get("previous")
    if previous:
        page.goto(f"{base}/?day={day}", wait_until="networkidle")
        finger.drag(100, 420, 340, 420)
        settle(page)
        check("day-steps-older", f"day={previous}" in page.url, page.url)
    else:
        lines.append("SKIP day-steps-older: no older day")

    # a paper, stepped to the next in its list, which is its shelf's: two papers of one shelf;
    # then the edge swipe back, which returns to the first paper, as a step is a history entry
    page.goto(f"{base}/papers", wait_until="networkidle")
    pair = pair_of(page)
    hrefs = [a.get_attribute("href") or "" for a in page.locator(PAPER_LINK).all()]
    if pair:
        first, second = pair[0], pair[1]
        page.locator(f'main a[href="{first}"]').first.click()
        page.wait_for_url("**/paper/**")
        settle(page, 500)
        finger.drag(300, 420, 60, 420)
        if check("paper-steps-next", arrived(page, second), page.url):
            finger.drag(36, 420, 330, 420)
            check("edge-swipe-goes-back", arrived(page, first), page.url)
    else:
        # the stack seeds its papers in one shelf: no shelf of two is the list's failure, not a skip
        listed = f"no shelf of two papers, {len(hrefs)} listed"
        lines.append(f"FAIL paper-steps-next, edge-swipe-goes-back: {listed}")

    # a Calibrate session's cards
    sessions = context.request.get(f"{base}/api/settings/calibrate")
    assert sessions.ok, f"reading the Calibrate sessions: HTTP {sessions.status}"
    session = next((r for r in sessions.json() if r["papers"] >= 2), None)
    if session:
        page.goto(f"{base}/settings/calibrate/{session['id']}?at=1", wait_until="networkidle")
        # from the paper's own text, which takes no finger of its own: the Note field below may
        # sit anywhere, by the paper's length
        box = page.locator("main .calp").first.bounding_box()
        assert box, "the card shows no paper"
        y = box["y"] + min(40, box["height"] / 2)
        finger.drag(box["x"] + box["width"] - 50, y, box["x"] + 50, y)
        # a card with a standing Fit saves before the step: wait for it, within reason
        check("calibrate-steps-next", arrived(page, "at=2"), page.url)
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
        assert arrived(page, hrefs[0]), f"the paper's page did not show: {page.url}"
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


def main(base: str, token: str, out: str, day: str = "") -> None:
    shots = Path(out)
    shots.mkdir(parents=True, exist_ok=True)
    lines = gestures(base, token, shots, day or date.today().isoformat())
    for line in lines:
        print(line)
    failures = [line for line in lines if line.startswith("FAIL")]
    print(f"{len(failures)} failures; screenshots in {shots}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main(*sys.argv[1:5])
