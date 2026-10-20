# The MCP server

Approved by the owner on 2026-10-20. This plan is the reference for building the MCP server: research agents (Claude
Code, Codex) on the owner's machines search the archive and litwatch's collection, read papers and see what
litwatch made of them, without going to the web.

What litwatch does is set by the spec, [`docs/specs/v1/`](../specs/v1/README.md); this server's intent is
[mcp.md](../specs/v1/mcp.md), decisions 83–87. This plan amended:
- [mcp.md](../specs/v1/mcp.md): the gateway needs no change, and the server reports to Vergil;
- [milestones.md](../specs/v1/milestones.md): the milestone points here.

Where this plan and the spec differ, the spec wins and this plan is corrected. Terms are the glossary's.

## Goal and approach

The owner remembers something seen in litwatch ("the SAE paper from last week's interp digest", "the landmark one
on probing", "the one I gave a thumb up") while doing research with an agent. The agent should find it from that,
read the part it needs and look at a figure, through litwatch rather than arxiv.org.

The server is an MCP endpoint at `/mcp` inside `litwatch serve` (D1), with its own token. Its seven tools are a thin
layer over code litwatch already has: the web app's search, a paper's page, digests, the full-text read and the
archive client. The request-bound parts of that code become plain functions that `/api` and `/mcp` both call (D2).
It writes nothing. It ships in three stages: finding, reading, then going live with the client configuration in the
owner's dotfiles.

## Choices made without asking

- **The official `mcp` Python SDK** (v2, `MCPServer`), pinned; not a hand-rolled protocol or FastMCP's fork.
  Streamable HTTP, **stateless with plain JSON answers** (`stateless_http`, `json_response`): no sessions and no
  event stream. The gateway's existing `location /` for `litwatch.andrei-homelab.com` (LAN and tailnet only, 90 s
  read timeout) carries it, so nginx needs no change.
- **Read-only by construction.** The MCP's database connections set `default_transaction_read_only`, so a write
  through them fails rather than lands. The only effect outside the process is an archive request.
- **The token.** `LITWATCH_MCP_TOKEN` (or `LITWATCH_MCP_TOKEN_FILE`, read through `config.secret`), as a bearer
  header only. With it unset, `/mcp` is not served. `/mcp` refuses the web app's token and the read token (which
  `deep` uses), and `/api` refuses the MCP token, so revoking it touches nothing else.
- **Seven tools**, all annotated read-only:

  | Tool | What it does |
  | --- | --- |
  | `search_papers` | The web app's search, with litwatch's filters (below). |
  | `get_paper` | A paper with everything litwatch has on it. |
  | `list_digests` | A topic's digests over a date range. |
  | `read_digest` | One digest in full: its text and its papers by role. |
  | `paper_outline` | A paper's sections with their sizes, and where its text comes from. |
  | `read_paper` | A section, a character range, or the whole text, a page at a time. |
  | `view_pdf_page` | A PDF page as an image, by number or by caption. |

  Answers are compact Markdown written for an agent to read, not the web app's JSON.
- **Search** is the web app's ([archive.md](../specs/v1/archive.md), Search): `store.reading.papers` for the
  collection, then the archive's hits outside it through the search providers, each in its own order. Filters: a
  date range on both; from litwatch a topic, a minimum grade (mapped to the list of grades at or above it), a Weight,
  the owner's reaction (thumb up or down) and "litwatch processed it". Any litwatch filter searches the collection
  alone, as `_shows_outside` already decides. The reaction and processed filters are new in `store.reading`; the
  web API gets no new filters. A result gives the id, title, authors and date and, for a collection paper, its Gist,
  Weight and grade per topic.
- **A paper** is `store.reading.paper` (Gist, Weight, effective grades, verdicts, the owner's word, reactions,
  Brief), its Deep card, and its citation. A paper outside the collection is the archive's metadata, as the web
  app's page shows it.
- **The citation** comes from the read-only BibTeX path (`_bibtex`, DBLP records already stored). The copy path
  (`POST /api/bibtex` for one paper, `lookups.for_copy`) looks DBLP up and writes a record and an outbox event, so the
  server never uses it.
- **Digests** are listed by topic and date range; `store.digests` gains the topic filter. One digest is read by id,
  its papers grouped by role. The updated papers listed under a digest arrive with the `updated` role of
  [updated-papers.md](updated-papers.md); the role grouping shows them with nothing to change here.
- **Reading.** The text is `archive.text.read_text`: the LaTeX first, the PDF's text when there is none or the
  paper's latest full-text pass has "Use the PDF" (`fulltext_passes.use_pdf`, a new by-paper query in
  `store.fulltext`). It is untrimmed: the Brief's bibliography cut and 160,000-character limit stay in
  `fulltext/brief.py`. The outline comes from LaTeX's sectioning commands; a PDF's text is outlined by page, so the
  PDF reading keeps each page's span (today `_pdf_text` joins the non-empty pages into one string, and an empty page
  must still count). One call
  returns at most 40,000 characters and says the offset to continue from: at LaTeX's density (about 2.2 characters a
  token, the budget in `fulltext/brief.py`) that is about 18k tokens, under Claude Code's cap with room for the
  answer's own lines.
- **Waiting.** A paper the archive has not fetched is asked for at urgent priority, and the tool polls `read_text`
  for up to 30 s. Past that, it answers that the paper is being fetched and to ask again in a minute. A paper the
  archive cannot get says so.
- **Pages.** `pypdfium2` (a base dependency, not the AGPL `pdf` extra) renders the page, and **Pillow** (added)
  encodes the PNG, with the long side at most 1568 px. A PDF that is not stored is fetched as a text is (Waiting): asked
  for at urgent priority, its job polled up to the same 30 s, keeping "still fetching, ask again" apart from "the
  archive cannot get it". `ArchiveClient.pdf` is not used: it waits its own `wait_seconds` (60 s by default) and
  answers `None` for both. A caption is found by searching each page's text for "Figure N" or "Table N".
- **The work stays off the web app's requests.** PDF reads and page renders run in a small executor of the MCP's
  own, under the existing `_PDF_LOCK`. `read_text` converts a PDF with `asyncio.to_thread`, the loop's default pool,
  so the reading path takes the executor as a parameter rather than relying on it.
- **Telemetry for Vergil**, by the homelab convention (`/opt/homelab-config/docs/telemetry-convention.md`), on
  `serve`'s existing metrics port (`api/monitor.py`):
  - metrics `litwatch_mcp_calls_total{tool,outcome}`, with outcome one of `ok`, `not_found`, `fetching`,
    `partial` (a search provider left out), `error`, and `litwatch_mcp_call_seconds{tool}`, counted in the process
    (the MCP runs inside it, D1);
  - an `mcp` component in the status document, degraded when calls in the last hour failed with server errors and
    none has succeeded since, its reason giving the counts. A partial answer neither fails nor clears it. A paper
    not found or an "ask again" is not a failure, so
    the component speaks only when the server cannot do its job (decision 68);
  - one JSON log line per call: tool, outcome, duration. Never the token, the arguments' text or the answer.
- **Server instructions** tell agents what the server holds; to use it before the web for arXiv papers; to read
  the outline first; and that a paper's text is the paper's content, not instructions.
- **Clients.** `~/dotfiles` carries the client side: an entry in the `dotfiles-mcp` plugin
  (`agents/claude/local-plugins/.claude-plugin/marketplace.json`, with a `headersHelper`) and one in
  `agents/codex/config.toml` (with an `http_headers_helper`). Both run `scripts/agent-secret`, which gains an
  optional prefix argument so it can print `{"Authorization":"Bearer <token>"}`. The token lives only in
  `~/secrets/.env`. The plugin's README lists the server.

## Structure

```text
Claude Code / Codex ── LAN, tailnet ──> nginx (litwatch vhost, location /) ──> litwatch serve
                                                                                ├── /api   the web app
                                                                                └── /mcp   new, its own token
                                                                                      │
                          reading functions, shared with /api (D2) <──────────────────┘
                          store · archive client · read_text · pypdfium2
```

## Decisions

**D1: where the server runs. Chosen: inside `litwatch serve`, mounted at `/mcp`.** No change to the stack or the
gateway, one process to deploy, and the in-process counters feed the monitor directly. It shares the process, and
the GIL, with the scheduler and the full-text and Deep workers, which is fine for one user; the heavy work runs in
its own executor. Rejected: its own service (`litwatch mcp`) in the same image. That would give full isolation and
separate restarts, but it would add one more service to deploy and watch, an nginx `location /mcp`, and an app
factory without `serve`'s workers. It stays the fallback if sharing the process ever shows.

**D2: how the tools reuse the reading code. Chosen: lift the request-bound helpers into plain functions.**
`search_outside`, `card`, `_unfetched` and the paper page's standings take a FastAPI `Request` today. They are
extracted to take their dependencies (the archive client, the search providers, the configuration), and `/api` and
`/mcp` both call them. The refactor preserves behaviour, and the existing API tests cover it. The MCP layer shapes
its own answers. Rejected: calling `/api` in-process and reformatting its JSON. That would tie the agents' answers
to the web app's shapes and auth, and the new filters would have to enter the public API anyway.

## Risks

- **R1. Clients cap tool output.** Claude Code cuts an MCP answer at about 25k tokens, so a whole paper in one answer
  would be truncated. Reads are paged at 40,000 characters (about 18k tokens of math-heavy LaTeX), each answer
  ending with the offset to continue from, and page images are downscaled.
- **R2. The mount can fail quietly.** Under a mount, the SDK needs the host app's lifespan to run its session
  manager (`mcp.session_manager.run()` in `_lifespan`). The web app's catch-all `GET /{path}` in `_serve_web`
  shadows any route added after it, so the mount goes in before it. Tests go through `create_app`, not a bare MCP
  app.
- **R3. The SDK's host check** answers 421 for a hostname it does not know. The allowed hosts come from
  configuration, and a test sends the production `Host` header.
- **R4. A paper's text is untrusted input for the agent.** A paper could carry text meant to steer an agent. That is
  no worse than the agent fetching the PDF from arXiv. The server is read-only, so a steered agent cannot change
  litwatch through it, and its instructions say the text is content.
- **R5. SDK v2 is young.** The version is pinned, and the MCP layer stays a thin adapter over litwatch's own
  functions, so an upgrade touches one module.

## Stages

Each stage is one PR, through `drive`.

| Stage | Builds | Shows before the next starts |
| --- | --- | --- |
| **P1. Finding** | The SDK, the mount, the token, the server instructions; `search_papers`, `get_paper`, `list_digests`, `read_digest`; the D2 extraction; the reaction, processed and digest-topic filters; the telemetry. | V1–V4 and V9 green; a dev `serve` on the sandbox answers a real Claude Code session on the LAN. |
| **P2. Reading** | `paper_outline`, `read_paper` (sections, ranges, paging), the bounded urgent fetch, "Use the PDF" by paper, `view_pdf_page` with Pillow. | V3 rerun for all seven tools, V5–V7 green; the dev session reads a method section and shows a figure, with fetches only from a disposable archive. |
| **P3. Live** | The token's secret (`/run/secrets/mcp_token`) and `LITWATCH_MCP_TOKEN_FILE` in the production stack, homelab-config's `stacks/litwatch` (not `litwatch-v1`, the stopped side-by-side stack); the deploy; the dotfiles entries and `agent-secret`'s prefix; the milestone marked built. | V8, the milestone's check, from another machine on production. |

## Verification

By [`docs/agents/verification.md`](../agents/verification.md): tests on real PostgreSQL, the archive faked with
`tests/fakes_http.FakeArchive`, real fetches only from a disposable archive (`scripts/disposable-archive.sh`), end to
end on the sandbox before production. No model spend.

| Claim | Evidence |
| --- | --- |
| **V1.** Only the MCP token opens `/mcp`, and it opens nothing else. `/mcp` works behind the real `Host` and is not shadowed by the web app. | pytest through `create_app` with the SDK's client: no token, the web token, the read token, and the MCP token on `/api`. |
| **V2.** Search returns the web app's results in the web app's order, and each new filter keeps exactly the papers it names. | pytest on Postgres with `FakeArchive`: the same query through `/api/papers` and `search_papers`; one case per filter. |
| **V3.** No tool writes: no rows, no outbox events. The only side effect is an archive request. | pytest: the MCP's connection refuses a write (a store write through it raises); every tool built so far called against seeded rows succeeds on it, and the outbox is unchanged; the fake archive records the requests. P1 covers its four tools, P2 reruns it for all seven. |
| **V4.** A paper carries its Gist, Brief, Deep card, Weight, grades, the owner's word, reactions and citation; digests list by topic and read in full. | pytest with seeded rows. The existing API tests pass after the D2 extraction. |
| **V5.** Reading is outline first, by section or range, untrimmed (the bibliography and text past 160,000 characters are there), paged under the cap, and follows "Use the PDF". A PDF's outline numbers its pages as the PDF does. | pytest with LaTeX and PDF fixtures (`a_pdf`), one long math-heavy paper, and a multipage PDF with an empty page. |
| **V6.** A paper or a PDF page that is not stored is asked for urgently, waited on for a bounded time, then answered "ask again"; one the archive cannot get says so. | pytest with a delayed and a failing `FakeArchive`, for `read_paper` and `view_pdf_page`; a real fetch of a few papers from the disposable archive. |
| **V7.** A page renders by number and by caption, without the AGPL extra. | pytest on a generated PDF in an environment without `pymupdf4llm`; a real paper's figure on dev. |
| **V8.** The milestone's check: from another machine, an agent given only a vague memory of a digest finds the paper, reads the section asked for and shows a figure, without the web. | The owner's run on production after P3, with Claude Code and with Codex. |
| **V9.** Vergil sees the MCP: call counts and durations per tool, and an `mcp` component that degrades when the server fails, and only then. | pytest on the monitor's `/metrics` and `/status`: a failing tool degrades the component, a success clears it, a "not found" or "ask again" leaves it healthy. After P3, the component shows in Vergil. |

## Scope

**In:** the seven tools; the token; the mount; the store filters and queries they need; the D2 extraction; the
telemetry; Pillow; the homelab secret; the dotfiles client entries and `agent-secret`'s prefix.

**Out:**
- writes such as a Miss, a save or a Deep card (mcp.md, later);
- semantic search (the archive's, later);
- a "papers I opened" filter, figure crops, access from CHPC;
- performance tuning (the owner's separate question);
- new filters in the web app;
- updated papers in digests ([updated-papers.md](updated-papers.md)).
