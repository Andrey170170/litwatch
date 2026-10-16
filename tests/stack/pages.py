"""The stack smoke test's walk (docs/agents/verification.md): every page of the web app at phone
and desktop widths in headless Chromium, failing on a console error, an uncaught exception, a 5xx
answer or an empty page, with a screenshot of each kept in OUT. Then a Deep card asked for
through the API must come back from the deep service with live "Among the collection" rows.

    python pages.py BASE_URL TOKEN PAPERS_JSON OUT

Needs `playwright` and its Chromium (`playwright install chromium`)."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

WIDTHS = {"phone": (390, 844), "desktop": (1440, 900)}
CARD_WAIT_SECONDS = 120


def pages(paper: str, topic: str) -> dict[str, str]:
    """Every page there is, by name. W4 grows this list (milestones.md, N7)."""
    return {
        "digest": "/",
        "papers": "/papers",
        "search": "/papers?q=language%20model",
        "topics": "/topics",
        "topic": f"/topics/{topic}",
        "paper": f"/paper/{paper}",
    }


def watch(page) -> list[str]:
    """The page's errors as they happen: console errors, uncaught exceptions, 5xx answers, and
    401 or 403 ones (a lost session shows the token prompt, which has text of its own)."""
    seen: list[str] = []
    page.on("console", lambda m: m.type == "error" and seen.append(f"console error: {m.text}"))
    page.on("pageerror", lambda e: seen.append(f"uncaught: {e}"))
    page.on(
        "response",
        lambda r: (
            (r.status >= 500 or r.status in (401, 403)) and seen.append(f"HTTP {r.status} {r.url}")
        ),
    )
    return seen


def walk(base: str, token: str, paper: str, topic: str, out: Path) -> list[str]:
    """Visit every page at each width; return what went wrong."""
    failures: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for width, (w, h) in WIDTHS.items():
            context = browser.new_context(viewport={"width": w, "height": h})
            signed = context.request.post(f"{base}/api/session", data={"token": token})
            assert signed.ok, f"signing in: HTTP {signed.status}"
            page = context.new_page()
            seen = watch(page)
            for name, path in pages(paper, topic).items():
                seen.clear()
                page.goto(base + path, wait_until="networkidle")
                # The desktop shell's <main>, or the phone shell's screen (it has no <main>).
                main = page.locator("main, .screen").first
                text = main.inner_text().strip() if main.count() else ""
                if not text:
                    seen.append("empty page: no text in the page's content")
                page.screenshot(path=out / f"{name}-{width}.png", full_page=True)
                failures += [f"{name} at {width} ({path}): {s}" for s in seen]
            context.close()
        browser.close()
    return failures


def card(base: str, token: str, paper: str, collection: set[str]) -> list[str]:
    """Ask for a Deep card and wait for it: its among rows must name collection papers."""
    from urllib.request import Request, urlopen  # noqa: PLC0415

    auth = {"Authorization": f"Bearer {token}"}
    urlopen(Request(f"{base}/api/papers/{paper}/card", method="POST", headers=auth), timeout=30)
    deadline = time.monotonic() + CARD_WAIT_SECONDS
    while time.monotonic() < deadline:
        with urlopen(Request(f"{base}/api/papers/{paper}", headers=auth), timeout=30) as response:
            found = json.load(response)
        if found["card"]:
            among = found["card"]["among"]
            if not among:
                return ["the card has no Among the collection rows"]
            strays = [
                a["id"]
                for a in among
                if not a["in_collection"] or a["id"] not in collection or a["id"] == paper
            ]
            print(f"card: {len(among)} among rows: {[a['id'] for a in among]}")
            return [f"among rows outside the collection: {strays}"] if strays else []
        state = found["card_pass"] or {}
        if state.get("state") == "failed":
            return [f"the card pass failed: {state.get('error')}"]
        time.sleep(2)
    return [f"no card within {CARD_WAIT_SECONDS}s: {found['card_pass']}"]


def topic_lists(base: str, token: str, topic: str, collection: set[str]) -> list[str]:
    """The topic's page must list the seeded papers classified into it."""
    from urllib.request import Request, urlopen  # noqa: PLC0415

    auth = {"Authorization": f"Bearer {token}"}
    with urlopen(Request(f"{base}/api/topics/{topic}", headers=auth), timeout=30) as response:
        rows = {r["id"] for r in json.load(response)["rows"]}
    return [] if rows & collection else [f"the {topic} page lists none of the seeded papers"]


def main(base: str, token: str, fixture: str, out: str) -> None:
    papers = [item["id"] for item in json.loads(Path(fixture).read_text())]
    shots = Path(out)
    shots.mkdir(parents=True, exist_ok=True)
    # smoke.sh classified every seeded paper into the seed taxonomy's language-models Field topic.
    failures = card(base, token, papers[0], set(papers))
    failures += topic_lists(base, token, "language-models", set(papers))
    failures += walk(base, token, papers[0], "language-models", shots)
    for failure in failures:
        print("FAIL", failure)
    print(f"{len(failures)} failures; screenshots in {shots}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main(*sys.argv[1:5])
