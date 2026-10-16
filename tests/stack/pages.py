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
from datetime import date, timedelta
from pathlib import Path

from playwright.sync_api import sync_playwright

WIDTHS = {"phone": (390, 844), "desktop": (1440, 900)}
CARD_WAIT_SECONDS = 120
# A paper neither litwatch nor the archive has: the one 404 the walk expects.
# an id no paper has, shaped as a real one (the share target reads a month into it)
MISSING_ID = "0001.00001"


def pages(paper: str, topic: str, day: str, missing: str = MISSING_ID) -> dict[str, str]:
    """Every page there is, by name (web/src/router.tsx): the Digest's views, the lists, a paper
    in the collection and one neither litwatch nor the archive has, the share target with each,
    and every Settings page with the pages inside them. A digest's own page and a Calibrate
    session need a digest written or a session drawn, which the smoke stack has not; `walk` adds
    them when the API has one."""
    return {
        "digest": "/",
        "digest-day": f"/?day={day}",
        "week": "/week",
        "history": "/history",
        "history-calendar": "/history?view=cal",
        "papers": "/papers",
        "search": "/papers?q=language%20model",
        "topics": "/topics",
        "topic": f"/topics/{topic}",
        "paper": f"/paper/{paper}",
        "paper-outside": f"/paper/{missing}",
        "share": f"/share?url=https%3A%2F%2Farxiv.org%2Fabs%2F{paper}",
        "share-outside": f"/share?text=see%20arxiv.org%2Fabs%2F{missing}",
        "settings": "/settings",
        "settings-overview": "/settings/overview",
        "settings-needs": "/settings/needs",
        "settings-calibrate": "/settings/calibrate",
        "settings-misses": "/settings/misses",
        "settings-topics": "/settings/topics",
        "settings-topic-editor": f"/settings/topics/{topic}",
        "settings-taxonomy": "/settings/taxonomy",
        "settings-runs": "/settings/runs",
        "settings-jobs": "/settings/runs/jobs",
        "settings-integrations": "/settings/integrations",
        "settings-integration-events": "/settings/integrations/archive",
        "settings-configuration": "/settings/configuration",
    }


NOT_A_PAGE = "There is no such page"
# What a page says when what it was asked for is missing; a seeded page saying so has lost its data.
MISSING = ("There is no such page", "No such digest", "is not in the collection")
# The pages that are missing something by design.
MAY_MISS = {"paper-outside", "share-outside"}


def expect(paper: str, day: str, width: str) -> dict[str, tuple[str, str]]:
    """What a page must show to count as working, beyond text in <main> that says nothing is
    missing (`MISSING`): an element of its own, or a word. The Digest's views show their place
    (Today in the Day's stepper, the day asked for, the Week in the phone's bar or the desktop's
    view tabs, History's active form); the lists show their data (Papers a paper link, a shelf
    folded on its count, or its loaded empty state: a fresh stack's papers are classified by hand
    and ungraded, so common, in the shelf folded by default; an archive search's heading, a topic
    card, the topic's heading); a paper shows its id, or Process when
    outside the collection; the share target's landing has the paper's sheet open on its labels on
    the phone (the desktop's rail is open already), or the picker of Report as a miss open when the
    shared paper is one neither litwatch nor the archive has; the Settings pages their titles. A
    `locator` is looked for in the whole page (a phone's bar and foot lie outside <main>); `text`
    in <main>, without case (`inner_text` renders a button's CSS `text-transform`)."""
    d = date.fromisoformat(day)
    phone = width == "phone"
    return {
        "digest": ("locator", '.stp .lbl small:has-text("Today")'),
        "digest-day": ("locator", f'.stp .lbl:has-text("{d:%a} {d.day}")'),
        "week": (
            "locator",
            '.ttl:has-text("Digest · Week")' if phone else '.vseg .on:has-text("Week")',
        ),
        "history": ("locator", '[aria-label="History as"] .on[aria-label="List"]'),
        "history-calendar": ("locator", '[aria-label="History as"] .on[aria-label="Calendar"]'),
        "papers": ("locator", 'main :is(a[href^="/paper/"], .shh .k:not(:empty), .empty)'),
        "search": ("locator", ".outh"),
        "topics": ("locator", ".tcard"),
        "topic": ("locator", ".thead .h1"),
        "paper": ("text", paper),
        "paper-outside": ("text", "Process"),
        "share": ("locator", ".sheet .slab") if phone else ("text", paper),
        "share-outside": ("locator", '[role="group"][aria-label="Report as a miss"]'),
        "settings": ("text", "Overview"),
        "settings-overview": ("text", "Overview"),
        "settings-needs": ("text", "Needs you"),
        "settings-calibrate": ("text", "Calibrate"),
        "settings-calibrate-session": ("locator", '[aria-label="Papers in the session"]'),
        "settings-calibrate-review": (
            "locator",
            'button:has-text("Add the papers waiting for a word")',
        ),
        "settings-misses": ("text", "Misses"),
        "settings-topics": ("text", "Topics"),
        "settings-topic-editor": ("text", "follows"),
        "settings-taxonomy": ("text", "Taxonomy"),
        "settings-runs": ("text", "Runs and cost"),
        "settings-jobs": ("text", "Jobs"),
        "settings-integrations": ("text", "Integrations"),
        "settings-integration-events": ("text", "events"),
        "settings-configuration": ("text", "Configuration"),
        "digest-one": ("locator", ".dgfull .dghead .h1 a"),
    }


def watch(page) -> list[str]:
    """The page's errors as they happen: console errors, uncaught exceptions, 5xx answers, 401 or
    403 ones (a lost session shows the token prompt, which has text of its own), and 404s, which a
    missing asset or a wrong API path make. The read of `MISSING_ID`, which the walk asks for, is
    the one 404 expected, and the browser's console note of it goes with it."""
    seen: list[str] = []
    expected = lambda url: MISSING_ID in url  # noqa: E731
    page.on(
        "console",
        lambda m: (
            m.type == "error"
            and not expected(m.location.get("url", ""))
            and seen.append(f"console error: {m.text}")
        ),
    )
    page.on("pageerror", lambda e: seen.append(f"uncaught: {e}"))
    page.on(
        "response",
        lambda r: (
            (r.status >= 500 or r.status in (401, 403) or (r.status == 404 and not expected(r.url)))
            and seen.append(f"HTTP {r.status} {r.url}")
        ),
    )
    return seen


def found_pages(context, base: str, day: str) -> dict[str, str]:
    """The pages that exist only once the data does: the page of a digest written around `day`
    (a day's or a week's), the first Calibrate session, on its card or its review. A read that
    fails is an error, not an absence."""
    extra: dict[str, str] = {}
    since = (date.fromisoformat(day) - timedelta(days=30)).isoformat()
    until = (date.fromisoformat(day) + timedelta(days=7)).isoformat()
    history = context.request.get(f"{base}/api/digests?since={since}&until={until}")
    assert history.ok, f"reading the digest history: HTTP {history.status}"
    found = history.json()
    marks = [m for d in found["days"] + found["weeks"] for m in d["marks"]]
    if marks:
        extra["digest-one"] = f"/digests/{marks[0]['id']}"
    sessions = context.request.get(f"{base}/api/settings/calibrate")
    assert sessions.ok, f"reading the Calibrate sessions: HTTP {sessions.status}"
    if rows := sessions.json():
        # a session with a paper left to answer opens on that paper's card; one answered through,
        # on its review
        s = rows[0]
        kind = "review" if s["answered"] >= s["papers"] else "session"
        extra[f"settings-calibrate-{kind}"] = f"/settings/calibrate/{s['id']}"
    return extra


def walk(base: str, token: str, paper: str, topic: str, day: str, out: Path) -> list[str]:
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
            for name, path in (pages(paper, topic, day) | found_pages(context, base, day)).items():
                seen.clear()
                page.goto(base + path, wait_until="networkidle")
                # Both shells put the page in <main>.
                main = page.locator("main").first
                text = main.inner_text().strip() if main.count() else ""
                if not text:
                    seen.append("empty page: no text in the page's content")
                elif main.locator(".loadmore", has_text="Could not load").count():
                    # A failed read renders its own line with "Try again": not a page that works.
                    seen.append("failed read: the page says it could not load")
                elif main.locator(".outh", has_text="did not answer").count():
                    # An archive search's heading names the providers that did not answer.
                    seen.append("failed read: the archive did not answer")
                for line in MISSING:
                    if line in text and (name not in MAY_MISS or line == NOT_A_PAGE):
                        seen.append(f"the page says {line!r}")
                kind, want = expect(paper, day, width)[name]
                if kind == "text" and want.lower() not in text.lower():
                    seen.append(f"the page does not say {want!r}")
                elif kind == "locator" and not page.locator(want).count():
                    seen.append(f"nothing matches {want}")
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
    items = json.loads(Path(fixture).read_text())
    papers = [item["id"] for item in items]
    shots = Path(out)
    shots.mkdir(parents=True, exist_ok=True)
    # smoke.sh classified every seeded paper into the seed taxonomy's language-models Field topic.
    failures = card(base, token, papers[0], set(papers))
    failures += topic_lists(base, token, "language-models", set(papers))
    failures += walk(base, token, papers[0], "language-models", items[0]["published"], shots)
    for failure in failures:
        print("FAIL", failure)
    print(f"{len(failures)} failures; screenshots in {shots}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main(*sys.argv[1:5])
