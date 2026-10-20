"""The MCP server's reading (docs/plans/mcp-server.md, V5–V7): outline first, a section, a range or
the whole text a page at a time, untrimmed, following "Use the PDF"; a PDF's text by page; the
bounded urgent fetch; PDF pages as images by number and by caption, without the AGPL extra.
Every call goes through `create_app` over HTTP, as an agent's does."""

import asyncio
import base64
import io
import re
import sys
import threading

import httpx
import pytest
from PIL import Image

from litwatch.api import mcp as mcp_server
from litwatch.api import mcp_reading
from litwatch.api.monitor import create_monitor
from litwatch.archive import pages
from litwatch.archive import text as text_module
from litwatch.archive.pages import MAX_SIDE
from litwatch.store import papers as papers_store
from litwatch.store import taxonomy as taxonomy_store
from tests.conftest import TREE, paper
from tests.fakes_http import FakeArchive, mock
from tests.test_archive_text import pages_pdf
from tests.test_mcp import Flaky, _app, call, ok, rpc, serving

PAGE = mcp_reading.PAGE_CHARS
MATH = (
    r"We bound $\|W_\ell x\|_2 \le \sigma_{\max}(W_\ell)\,\|x\|_2$ and "
    r"$\sum_{i=1}^{n} \alpha_i \nabla_\theta \log p_\theta(y_i \mid x_i)$ on every layer. "
)
# A long, math-heavy paper: its Results run past the Brief's 160,000-character cut, and its
# bibliography and appendix come after that.
LATEX = (
    "\\documentclass{article}\n% \\section{Commented out}\n\\begin{document}\n"
    "\\title{Probes}\\maketitle\n"
    "\\section{Introduction}\n" + MATH * 40 + "\n"
    "\\section*{Method \\& setup}\n" + MATH * 20 + "\n"
    "\\subsection{Probes {on} layers}\n" + MATH * 20 + "\n"
    "It reaches 50\\% \\subsection{After an escaped per cent}\n" + MATH * 20 + "\n"
    "A break \\\\% \\section{After a line break, so a comment}\n"
    "\\section{Results}\n" + MATH * 1300 + "\n"
    "\\begin{thebibliography}{9}\n\\bibitem{a} A. Author. Probing probes. 2020.\n"
    "\\end{thebibliography}\n\\appendix\n"
    "\\section{Proofs}\n" + MATH * 400 + "APPENDIX-TAIL-MARKER\n\\end{document}\n"
)
HELD = paper("2610.00011", title="Probes read every layer", version=2)
PDF_ONLY = paper("2610.00012", title="A scanned paper", version=1)
FIGURES = pages_pdf(
    ["Introduction. As Figure 2 shows, probes work."],
    [],
    ["Figure 2: Accuracy by layer.", "Table 1 compares them."],
    ["Table 1: Results on three tasks."],
)


class Delayed(FakeArchive):
    """The fake archive, answering every request about a job "pending" until it has answered
    `polls` of them (forever, with None), then "complete"."""

    def __init__(self, polls: int | None) -> None:
        super().__init__()
        self.polls, self.answered = polls, 0

    def handle(self, req: httpx.Request) -> httpx.Response:
        if req.url.path.startswith("/v1/jobs"):
            ready = self.polls is not None and self.answered >= self.polls
            self.status = "complete" if ready else "pending"
            self.answered += 1
        return super().handle(req)


@pytest.fixture(autouse=True)
def quick_polls(monkeypatch):
    monkeypatch.setattr(mcp_reading, "POLL_SECONDS", 0.05)


@pytest.fixture
async def shelf(conn):
    await taxonomy_store.save(conn, TREE)
    await papers_store.upsert(conn, [HELD, PDF_ONLY])


def reading_app(conn, fake: FakeArchive, seen: list | None = None, wait: float = 5.0):
    app = _app(conn, fake, seen)
    app.state.mcp_wait = wait  # read at each call: seconds, not the 30 a real call waits
    return app


async def view(http, **arguments) -> dict:
    """view_pdf_page's whole result, its image included."""
    answer = await rpc(http, "tools/call", {"name": "view_pdf_page", "arguments": arguments})
    result = answer.json()["result"]
    assert not result["isError"], result
    return result


def chunk(answer: str) -> str:
    """The text an answer of read_paper holds: between its three heading lines and its tail."""
    return answer.split("\n", 3)[3].rsplit("\n\n[", 1)[0]


def entries(outline: str) -> list[tuple[str, int, str, int]]:
    """An outline's entries: (indent, number, title, offset), with each size checked later."""
    found = re.findall(
        r"^( *)(\d+)\. (.+) · (?:[\d,]+ characters|blank) · offset (\d+)$", outline, re.M
    )
    return [(indent, int(n), title, int(at)) for indent, n, title, at in found]


async def pass_using_pdf(conn, paper_id: str, use_pdf: bool) -> None:
    await conn.execute(
        "INSERT INTO fulltext_passes (paper_id, paper_revision, kind, reason, state, priority, "
        "use_pdf) VALUES (%s, 1, 'brief', 'focus', 'done', 0, %s)",
        (paper_id, use_pdf),
    )


# -- V5: outline first, then a part, whole ---------------------------------------------------------


async def test_the_outline_lists_latex_sections_and_reads_them_whole_a_page_at_a_time(conn, shelf):
    fake = FakeArchive()
    fake.texts[HELD.id] = (2, LATEX)
    async with serving(reading_app(conn, fake)) as http:
        outline = await ok(http, "paper_outline", paper_id=HELD.id)
        listed = entries(outline)
        # Each entry, read by its number, is the text from its offset to the next of its depth.
        parts = {}
        for _, n, _, at in listed:
            first = await ok(http, "read_paper", paper_id=HELD.id, section=n)
            parts[n] = (first, at)
        results = next(n for _, n, title, _ in listed if title == "Results")
        paged, offset = [], None
        while True:  # the whole text, a page at a time, from wherever each answer says
            asked = {"offset": offset} if offset is not None else {}
            answer = await ok(http, "read_paper", paper_id=HELD.id, **asked)
            paged.append(chunk(answer))
            found = re.search(r"\[It goes on: read_paper with offset=(\d+)\.\]$", answer)
            if found is None:
                assert answer.endswith("[The end of the text.]")
                break
            offset = int(found[1])
        ranged = await ok(http, "read_paper", paper_id=HELD.id, offset=170_000, length=500)
        in_results = await ok(http, "read_paper", paper_id=HELD.id, section=results, offset=60_000)
    assert "arXiv:2610.00011v2 · its LaTeX source, " in outline and "whole" in outline
    assert [(indent, n, title) for indent, n, title, _ in listed] == [
        ("", 0, "Before the first heading"),
        ("", 1, "Introduction"),
        ("", 2, "Method \\& setup"),
        ("  ", 3, "Probes {on} layers"),
        ("  ", 4, "After an escaped per cent"),
        ("", 5, "Results"),
        ("", 6, "References (the bibliography)"),
        ("", 7, "Proofs"),
    ]
    starts = {n: at for _, n, _, at in listed}
    assert LATEX[starts[5] :].startswith("\\section{Results}")
    assert LATEX[starts[3] :].startswith("\\subsection{Probes {on} layers}")
    # A section holds its subsections; a page of it ends where the section does or says go on.
    method = chunk(parts[2][0])
    assert method == LATEX[starts[2] : starts[5]] and "After an escaped per cent" in method
    assert parts[2][0].endswith("[The end of 2. Method \\& setup.]")
    assert (
        len(chunk(parts[5][0])) == PAGE and f"section=5, offset={starts[5] + PAGE}" in parts[5][0]
    )
    assert chunk(in_results) == LATEX[60_000 : 60_000 + PAGE]
    # Untrimmed: the bibliography and the text past 160,000 characters are there.
    assert "\\bibitem{a} A. Author. Probing probes." in chunk(parts[6][0])
    assert starts[7] > 160_000 and "APPENDIX-TAIL-MARKER" in "".join(paged)
    assert "".join(paged) == LATEX and all(len(p) <= PAGE for p in paged) and len(paged) > 5
    assert chunk(ranged) == LATEX[170_000:170_500]
    assert "characters 170000–170500 of" in ranged


async def test_reading_follows_the_owners_use_the_pdf_on_the_latest_pass(conn, shelf, monkeypatch):
    monkeypatch.setitem(sys.modules, "pymupdf4llm", None)
    fake = FakeArchive()
    fake.texts[HELD.id] = (2, LATEX)
    fake.pdfs[HELD.id] = pages_pdf(["The words of the PDF."])
    await pass_using_pdf(conn, HELD.id, True)
    async with serving(reading_app(conn, fake)) as http:
        from_pdf = await ok(http, "paper_outline", paper_id=HELD.id)
        read = await ok(http, "read_paper", paper_id=HELD.id, section=1)
        asked = [j["kind"] for j in fake.jobs]
        await pass_using_pdf(conn, HELD.id, False)  # a later pass, read from the LaTeX
        latex = await ok(http, "paper_outline", paper_id=HELD.id)
    assert 'its PDF\'s text (the owner chose "Use the PDF" for it), 1 page,' in from_pdf
    assert chunk(read) == "The words of the PDF." and set(asked) == {"pdf"}
    assert "its LaTeX source" in latex and "1. Introduction" in latex


async def test_a_pdfs_text_is_outlined_by_page_as_the_pdf_numbers_them(conn, shelf, monkeypatch):
    monkeypatch.setitem(sys.modules, "pymupdf4llm", None)
    fake = FakeArchive()
    fake.statuses["source"] = "unavailable"
    fake.pdfs[PDF_ONLY.id] = FIGURES
    async with serving(reading_app(conn, fake)) as http:
        outline = await ok(http, "paper_outline", paper_id=PDF_ONLY.id)
        third = await ok(http, "read_paper", paper_id=PDF_ONLY.id, section=3)
        blank = await ok(http, "read_paper", paper_id=PDF_ONLY.id, section=2)
    assert "its PDF's text (it has no usable LaTeX source), 4 pages" in outline
    assert [(n, title) for _, n, title, _ in entries(outline)] == [
        (1, "Page 1"), (2, "Page 2"), (3, "Page 3"), (4, "Page 4"),
    ]  # fmt: skip
    assert "2. Page 2 · blank" in outline and "4. Page 4 · 32 characters" in outline
    assert chunk(third) == "Figure 2: Accuracy by layer.\nTable 1 compares them."
    assert chunk(blank) == "(blank)" and blank.endswith("[The end of 2. Page 2.]")


async def test_paragraphs_are_outlined_below_subsubsections(conn, shelf):
    method = (
        "\\section{Method}\nWe train.\n\\paragraph{Training setup}\nAdam.\n"
        "\\subparagraph*{Batch size}\n512.\n\\paragraph{Evaluation}\nThree tasks.\n"
    )
    fake = FakeArchive()
    fake.texts[HELD.id] = (2, method + "\\section{Results}\nIt works.\n")
    async with serving(reading_app(conn, fake)) as http:
        outline = await ok(http, "paper_outline", paper_id=HELD.id)
        setup = await ok(http, "read_paper", paper_id=HELD.id, section=2)
        whole = await ok(http, "read_paper", paper_id=HELD.id, section=1)
    assert [(indent, n, title) for indent, n, title, _ in entries(outline)] == [
        ("", 1, "Method"),
        ("      ", 2, "Training setup"),
        ("        ", 3, "Batch size"),
        ("      ", 4, "Evaluation"),
        ("", 5, "Results"),
    ]
    assert chunk(setup) == "\\paragraph{Training setup}\nAdam.\n\\subparagraph*{Batch size}\n512.\n"
    assert chunk(whole) == method  # a section holds its paragraphs


async def test_an_optional_argument_may_hold_a_bracket_inside_braces(conn, shelf):
    fake = FakeArchive()
    fake.texts[HELD.id] = (
        2,
        "\\section{Setup}\nData.\n"
        "\\section[{Analysis of $\\mathbb{E}[X]$}]{Method}\nWe bound it.\n"
        "\\subsection [short] {Lemma}\nProof.\n",
    )
    async with serving(reading_app(conn, fake)) as http:
        outline = await ok(http, "paper_outline", paper_id=HELD.id)
    assert [title for _, _, title, _ in entries(outline)] == ["Setup", "Method", "Lemma"]


async def test_a_heading_in_verbatim_text_is_an_example_not_a_heading(conn, shelf):
    method = (
        "\\section{Method}\nWe run:\n\\begin{verbatim}\n\\section{Results}\n% not a comment\n"
        "\\end{verbatim}\nand \\verb|\\section{Inline}| too.\n"
        "\\begin{lstlisting}\n\\subsection{Listed}\n\\end{lstlisting}\n"
        "\\begin{minted*}{latex}\n\\section{Minted}\n\\end{minted*}\n"
    )
    fake = FakeArchive()
    fake.texts[HELD.id] = (2, method + "\\section{Results}\nDone.\n")
    async with serving(reading_app(conn, fake)) as http:
        outline = await ok(http, "paper_outline", paper_id=HELD.id)
        whole = await ok(http, "read_paper", paper_id=HELD.id, section=1)
    assert [(n, title) for _, n, title, _ in entries(outline)] == [(1, "Method"), (2, "Results")]
    assert chunk(whole) == method  # the example stays inside its section


# -- V6: the bounded urgent fetch ------------------------------------------------------------------


async def _component(app) -> tuple[dict, str]:
    transport = httpx.ASGITransport(create_monitor(app))
    async with httpx.AsyncClient(transport=transport, base_url="http://m") as m:
        found = (await m.get("/status")).json()["components"]
        return next(c for c in found if c["id"] == "mcp"), (await m.get("/metrics")).text


async def test_a_paper_not_fetched_is_asked_for_urgently_and_waited_on_a_bounded_time(conn, shelf):
    slow = Delayed(polls=None)  # the archive never finishes, within the wait
    slow.texts[HELD.id] = (2, LATEX)
    app = reading_app(conn, slow, wait=0.3)
    async with serving(app) as http:
        for tool, arguments in [
            ("read_paper", {"paper_id": HELD.id}),
            ("view_pdf_page", {"paper_id": HELD.id, "page": 1}),
        ]:
            asked = len(slow.jobs)
            text, failed = await call(http, tool, **arguments)
            assert not failed and text.startswith(f"The archive is fetching arXiv:{HELD.id}'s")
            assert text.endswith("ask again in a minute."), text
            polled = slow.jobs[asked:]
            assert polled and all(j["priority"] == 0 for j in polled)  # urgent
        assert [j["kind"] for j in slow.jobs][-1] == "pdf"
        component, metrics = await _component(app)
    assert 'litwatch_mcp_calls_total{tool="read_paper",outcome="fetching"} 1' in metrics
    assert 'litwatch_mcp_calls_total{tool="view_pdf_page",outcome="fetching"} 1' in metrics
    assert component["status"] == "healthy"
    # One that arrives while the call waits is read, and shown.
    ready = Delayed(polls=3)
    ready.texts[HELD.id] = (2, LATEX)
    ready.pdfs[HELD.id] = FIGURES
    async with serving(reading_app(conn, ready)) as http:
        text = await ok(http, "read_paper", paper_id=HELD.id, length=100)
        read = [(j["kind"], j["status"]) for j in ready.jobs]
        ready.answered, ready.polls = 0, 2
        page = await view(http, paper_id=HELD.id, page=3)
    # The text asked for again until its source came; the PDF's job polled until it was done.
    assert chunk(text) == LATEX[:100] and read == [("source", "pending")] * 3 + [
        ("source", "complete")
    ]
    assert ready.jobs[-1]["kind"] == "pdf" and ready.answered == 3  # asked, polled twice
    assert [c["type"] for c in page["content"]] == ["text", "image"]


async def test_a_paper_the_archive_cannot_get_says_so_and_an_outage_is_an_error(conn, shelf):
    gone = FakeArchive(status="failed")  # its source and its PDF given up on
    flaky = Flaky()
    flaky.down = True
    app = reading_app(conn, gone)
    async with serving(app) as http:
        text, failed = await call(http, "read_paper", paper_id=HELD.id)
        assert not failed and text.startswith(f"The archive cannot get arXiv:{HELD.id}'s text: ")
        assert "source failed; PDF failed" in text
        page, failed = await call(http, "view_pdf_page", paper_id=HELD.id, page=1)
        assert (
            not failed and page == f"The archive cannot get arXiv:{HELD.id}'s PDF: its PDF failed."
        )
        component, metrics = await _component(app)
    assert component["status"] == "healthy"
    assert 'litwatch_mcp_calls_total{tool="read_paper",outcome="not_found"} 1' in metrics
    down = reading_app(conn, flaky)
    async with serving(down) as http:
        for tool, arguments in [
            ("paper_outline", {"paper_id": HELD.id}),
            ("view_pdf_page", {"paper_id": HELD.id, "caption": "Figure 2"}),
            ("read_paper", {"paper_id": "2610.05555"}),  # neither has it, as far as is known
        ]:
            text, failed = await call(http, tool, **arguments)
            assert failed and "try again in a minute" in text, (tool, text)
            assert "cannot get" not in text and "Neither" not in text
        component, _ = await _component(down)
        assert component["status"] == "degraded"
        # Still being fetched is an answer: it shows the server works (data.md, `mcp`).
        flaky.down, flaky.status = False, "pending"
        flaky.texts[HELD.id] = (2, LATEX)
        down.state.mcp_wait = 0.1
        text, failed = await call(http, "read_paper", paper_id=HELD.id)
        assert not failed and "fetching" in text
        component, _ = await _component(down)
    assert component["status"] == "healthy"


async def test_an_archive_that_stops_answering_after_pending_is_an_error_not_fetching(
    conn, shelf, monkeypatch
):
    """Only the wait's end after a pending answer says "fetching": a request that hangs past
    the bound, after pending or after the fetch is done, is an error the monitor sees."""
    monkeypatch.setattr(mcp_reading, "READ_SLACK", 0.3)
    fake = FakeArchive(status="pending")
    fake.texts[HELD.id] = (2, LATEX)
    hang_after = {"/v1/jobs": 1}  # a path's answers before it hangs; absent, it never does

    async def handle(req: httpx.Request) -> httpx.Response:
        key = next((k for k in hang_after if req.url.path.startswith(k)), None)
        if key is not None:
            if hang_after[key] <= 0:
                await asyncio.Event().wait()  # never answers
            hang_after[key] -= 1
        return fake.handle(req)

    app = reading_app(conn, fake, wait=0.2)
    async with serving(app) as http:
        app.state.archive.http = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        text, failed = await call(http, "read_paper", paper_id=HELD.id)
        assert failed and "did not finish in time" in text and "fetching" not in text
        # The PDF's job done, its download hangs.
        fake.status = "complete"
        hang_after.clear()
        hang_after[f"/v1/papers/{PDF_ONLY.id}/pdf"] = 0
        page, failed = await call(http, "view_pdf_page", paper_id=PDF_ONLY.id, page=1)
        assert failed and "did not finish in time" in page
        component, metrics = await _component(app)
    assert component["status"] == "degraded"
    assert 'litwatch_mcp_calls_total{tool="read_paper",outcome="error"} 1' in metrics
    assert 'outcome="fetching"} 0' in metrics and 'outcome="fetching"} 1' not in metrics


async def test_a_complete_pdf_job_with_no_pdf_served_is_an_error(conn, shelf):
    """The archive's inconsistency, not a paper without text: it degrades the component."""

    class NoFile(FakeArchive):
        def handle(self, req: httpx.Request) -> httpx.Response:
            if req.url.path.endswith("/pdf"):
                return httpx.Response(404)
            return super().handle(req)

    fake = NoFile()
    fake.statuses["source"] = "unavailable"
    app = reading_app(conn, fake)
    async with serving(app) as http:
        text, failed = await call(http, "read_paper", paper_id=PDF_ONLY.id)
        page, page_failed = await call(http, "view_pdf_page", paper_id=PDF_ONLY.id, page=1)
        component, metrics = await _component(app)
    assert failed and "no PDF served" in text and "try again in a minute" in text
    assert page_failed and "serves no PDF" in page
    assert component["status"] == "degraded"
    assert 'litwatch_mcp_calls_total{tool="read_paper",outcome="error"} 1' in metrics


async def test_a_job_answer_of_no_known_status_is_an_error_not_fetching(conn, shelf):
    """Only the archive's own waiting states are "fetching"; an answer without one says nothing
    of the file, so it is an error the monitor sees."""

    class Statusless(FakeArchive):
        def handle(self, req: httpx.Request) -> httpx.Response:
            answer = super().handle(req)
            if req.url.path.startswith("/v1/jobs"):
                return httpx.Response(200, json={"id": 1})
            return answer

    app = reading_app(conn, Statusless(), wait=0.2)
    async with serving(app) as http:
        text, failed = await call(http, "read_paper", paper_id=HELD.id)
        assert failed and "unknown status 'None'" in text, text
        app.state.archive.http = mock(FakeArchive(status="queued").handle)
        page, page_failed = await call(http, "view_pdf_page", paper_id=HELD.id, page=1)
        component, metrics = await _component(app)
    assert page_failed and "unknown status 'queued'" in page, page
    assert component["status"] == "degraded"
    assert 'outcome="fetching"} 1' not in metrics


# -- V7: a page as an image ------------------------------------------------------------------------


def _image(result: dict) -> tuple[str, Image.Image, bytes]:
    text, image = result["content"]
    assert image["type"] == "image" and image["mimeType"] == "image/png"
    png = base64.b64decode(image["data"])
    return text["text"], Image.open(io.BytesIO(png)), png


async def test_a_page_renders_by_number_and_by_caption_without_the_agpl_extra(
    conn, shelf, monkeypatch
):
    for name in ("pymupdf4llm", "pymupdf"):  # importing either fails, as without the extra
        monkeypatch.setitem(sys.modules, name, None)
    threads = []
    render = pages.render

    def recorded(*args):
        threads.append(threading.current_thread().name)
        return render(*args)

    monkeypatch.setattr(pages, "render", recorded)
    fake = FakeArchive()
    fake.pdfs[PDF_ONLY.id] = FIGURES

    async def shown(**arguments) -> dict:
        return await view(http, paper_id=PDF_ONLY.id, **arguments)

    async with serving(reading_app(conn, fake)) as http:
        by_number = {n: _image(await shown(page=n)) for n in (1, 3, 4)}
        figure = _image(await shown(caption="Figure 2"))
        fig = _image(await shown(caption="fig. 2"))
        table = _image(await shown(caption="Table 1"))
        missing = await call(http, "view_pdf_page", paper_id=PDF_ONLY.id, caption="Figure 9")
        past = await call(http, "view_pdf_page", paper_id=PDF_ONLY.id, page=9)
    text, image, png = by_number[3]
    assert text == f"# {PDF_ONLY.title}\narXiv:{PDF_ONLY.id}v1, page 3 of 4"
    assert max(image.size) == MAX_SIDE and image.size == (1212, MAX_SIDE)  # letter, upright
    # Figure 2's caption is on page 3, though page 1 names it first; Table 1's on page 4.
    assert figure[2] == fig[2] == png != by_number[1][2]
    assert figure[0].endswith("page 3 of 4: Figure 2's caption is on it")
    assert table[2] == by_number[4][2] and table[0].endswith(
        "page 4 of 4: Table 1's caption is on it"
    )
    assert missing == (f"arXiv:{PDF_ONLY.id}v1: none of its 4 pages names Figure 9.", False)
    assert past == (f"arXiv:{PDF_ONLY.id}v1: the PDF has 4 pages, so no page 9.", False)
    assert threads and all(t.startswith("mcp-pdf") for t in threads)  # off the default pool


async def test_a_pdfs_text_is_read_in_the_mcps_own_executor(conn, shelf, monkeypatch):
    threads = []
    read = text_module.read_pdf

    def recorded(pdf):
        threads.append(threading.current_thread().name)
        return read(pdf)

    monkeypatch.setitem(sys.modules, "pymupdf4llm", None)
    monkeypatch.setattr(text_module, "read_pdf", recorded)
    fake = FakeArchive()
    fake.statuses["source"] = "unavailable"
    fake.pdfs[PDF_ONLY.id] = FIGURES
    async with serving(reading_app(conn, fake)) as http:
        await ok(http, "paper_outline", paper_id=PDF_ONLY.id)
    assert threads and all(t.startswith("mcp-pdf") for t in threads)


# -- the arguments ---------------------------------------------------------------------------------


async def test_reading_arguments_out_of_bounds_are_refused_and_not_counted(conn, shelf):
    fake = FakeArchive()
    fake.texts[HELD.id] = (2, LATEX)
    app = reading_app(conn, fake)
    async with serving(app) as http:
        for tool, arguments in [
            ("read_paper", {"paper_id": HELD.id, "offset": 9223372036854775808}),
            ("read_paper", {"paper_id": HELD.id, "offset": -1}),
            ("read_paper", {"paper_id": HELD.id, "section": 100_001}),
            ("read_paper", {"paper_id": HELD.id, "length": PAGE + 1}),
            ("read_paper", {"paper_id": HELD.id, "length": 0}),
            ("read_paper", {"paper_id": HELD.id, "offset": len(LATEX) + 1}),
            ("read_paper", {"paper_id": HELD.id, "section": 8}),  # the outline ends at 7
            ("read_paper", {"paper_id": HELD.id, "section": 1, "offset": 0}),  # before it
            ("paper_outline", {"paper_id": f"{HELD.id}\u0000"}),
            ("view_pdf_page", {"paper_id": HELD.id}),
            ("view_pdf_page", {"paper_id": HELD.id, "page": 1, "caption": "Figure 1"}),
            ("view_pdf_page", {"paper_id": HELD.id, "caption": "the plot"}),
            ("view_pdf_page", {"paper_id": HELD.id, "caption": "Figure 1" + " " * 40}),
            ("view_pdf_page", {"paper_id": HELD.id, "page": 2**70}),
            ("view_pdf_page", {"paper_id": HELD.id, "page": 0}),
        ]:
            text, failed = await call(http, tool, **arguments)
            assert failed, (tool, arguments, text)
        component, metrics = await _component(app)
        at_end = await ok(http, "read_paper", paper_id=HELD.id, offset=len(LATEX))
    assert component["status"] == "healthy"
    assert re.search(r'outcome="(error|ok)"} [1-9]', metrics) is None
    assert chunk(at_end) == "(blank)" and at_end.endswith("[The end of the text.]")


async def test_an_appendix_or_supplement_caption_renders_dotted_or_not(conn, shelf):
    fake = FakeArchive()
    fake.pdfs[PDF_ONLY.id] = pages_pdf(
        ["Body text; Table A.1 and Figure A2 are in the appendix."],
        ["Table A.1: Hyperparameters."],
        ["Figure A.2: Loss curves."],
        ["Table S3: Supplementary results."],
    )
    async with serving(reading_app(conn, fake)) as http:
        found = {
            asked: _image(await view(http, paper_id=PDF_ONLY.id, caption=asked))[0]
            for asked in ("Table A.1", "Table A1", "Figure A.2", "fig. A2", "Table S3")
        }
        none = await call(http, "view_pdf_page", paper_id=PDF_ONLY.id, caption="Table A.10")
    for asked, page in [("Table A.1", 2), ("Table A1", 2), ("Figure A.2", 3), ("fig. A2", 3),
                        ("Table S3", 4)]:  # fmt: skip
        assert f"page {page} of 4: " in found[asked] and "caption is on it" in found[asked]
    assert none[1] is False and "none of its 4 pages names Table A.10" in none[0]


# A heading's title as long as an outline shows one, math-heavy.
LONG_TITLE = r": a bound on $\sum_{i=1}^{n} \alpha_i \|W_\ell x\|_2 \le \sigma_{\max}(W_\ell)$" * 4


@pytest.mark.parametrize("title", ["", LONG_TITLE], ids=["short titles", "long titles"])
async def test_a_long_outline_is_listed_in_parts_each_saying_where_to_go_on(conn, shelf, title):
    """By at most 300 entries an answer, and no more than 40,000 characters: with long titles,
    the characters end a part before the count does."""
    many = "Front matter.\n" + "".join(
        f"\\subsection{{Lemma {k}" + title + "}\nProof.\n" for k in range(650)
    )
    fake = FakeArchive()
    fake.texts[HELD.id] = (2, many)
    app = reading_app(conn, fake)
    async with serving(app) as http:
        listed, start, answers = [], None, 0
        while True:
            asked = {"start": start} if start is not None else {}
            answer = await ok(http, "paper_outline", paper_id=HELD.id, **asked)
            answers += 1
            shown = [n for _, n, _, _ in entries(answer)]
            assert len(shown) <= mcp_server.MAX_LISTED and len(answer) <= PAGE
            listed += shown
            found = re.search(r"call paper_outline again with start=(\d+)\.\]$", answer)
            if found is None:
                break
            start = int(found[1])
        past, failed = await call(http, "paper_outline", paper_id=HELD.id, start=651)
        component, metrics = await _component(app)
    assert listed == list(range(651))
    assert answers == 3 if not title else answers > 3
    assert failed and "No entry 651: the outline numbers 0–650." in past
    assert 'tool="paper_outline",outcome="error"} 0' in metrics
    assert component["status"] == "healthy"


async def test_a_caption_line_wins_however_its_label_is_set_off(conn, shelf):
    fake = FakeArchive()
    fake.pdfs[PDF_ONLY.id] = pages_pdf(
        ["As Figure 2 shows, and Figure 3 too."],
        ["Figure 2 | Accuracy by layer."],
        ["Figure 3 Loss curves by step."],
    )
    async with serving(reading_app(conn, fake)) as http:
        two = _image(await view(http, paper_id=PDF_ONLY.id, caption="Figure 2"))[0]
        three = _image(await view(http, paper_id=PDF_ONLY.id, caption="Figure 3"))[0]
    assert two.endswith("page 2 of 3: Figure 2's caption is on it")
    assert three.endswith("page 3 of 3: Figure 3's caption is on it")


async def test_roman_and_single_letter_captions_match_whole(conn, shelf):
    fake = FakeArchive()
    fake.pdfs[PDF_ONLY.id] = pages_pdf(
        ["Tables I, II and IV are below; see Figure A."],
        ["TABLE I: Setup."],
        ["TABLE II: Data."],
        ["TABLE IV: Results."],
        ["Figure A.1: Detail."],
        ["Figure A: Overview."],
    )
    async with serving(reading_app(conn, fake)) as http:
        found = {
            asked: _image(await view(http, paper_id=PDF_ONLY.id, caption=asked))[0]
            for asked in ("TABLE IV", "Table I", "table ii", "Figure A", "Figure A.1")
        }
    for asked, page in [("TABLE IV", 4), ("Table I", 2), ("table ii", 3), ("Figure A", 6),
                        ("Figure A.1", 5)]:  # fmt: skip
        assert f"page {page} of 6: " in found[asked] and "caption is on it" in found[asked], asked
