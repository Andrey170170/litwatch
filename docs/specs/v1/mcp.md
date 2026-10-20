# The MCP server

An MCP server through which the owner's research agents (Claude Code, Codex, on any of the owner's machines) find
papers, read them and see what litwatch made of them, instead of going to the web. Decisions 83–87.

## Why

The owner does research with agents and wants them to read papers easily when needed. The archive already holds
almost all of arXiv, and LaTeX text for every paper intake registers; an agent fetching the same paper from arxiv.org
duplicates it, and reads a PDF worse than litwatch already does.

litwatch also knows what the owner has seen. The owner notices a paper in a digest or on a topic's page, and weeks
later, mid-research, remembers it only vaguely: "the SAE paper from last week's interp digest", "the landmark one on
probing", "the one I gave a thumb up". The agent should find it from that and read it.

## What agents can do

Read-only (decision 84): the server writes nothing to litwatch's database or the outbox. Its one side effect is
asking the archive for a paper's files at urgent priority, as opening a PDF in the web app does.

- **Search.** The web app's search ([archive.md](archive.md), Search), lexical (decision 87): the collection's papers
  from litwatch's own index, the rest from the archive, each in its own order, exact title and phrase first. Optional
  filters: a date range, which applies to both, and from litwatch a topic, papers litwatch processed, a minimum grade,
  a Weight, papers the owner reacted to; any of these searches litwatch's index alone. A result gives a paper's id,
  title, authors and date and, where litwatch processed it, its Gist, Weight and grade per topic.
- **A paper.** Its metadata and abstract, and what litwatch has on it: Gist, Brief and Deep card where they exist,
  its Weight, the effective Fit and grade per topic, the owner's word and reactions, and its citation (the published
  version where litwatch found one).
- **Digests.** A topic's digests over a date range, and one digest read in full: its text, the papers it puts
  forward and the updated papers listed under them.
- **Reading.** Outline first (decision 85): the paper's sections with their sizes and where the text comes from
  (LaTeX or the PDF), then a section, a range or the whole text, so an agent after the method does not spend its
  context on the related work. The text is the source a full-text pass reads, the LaTeX first and the PDF's text when
  there is none or the owner chose "Use the PDF", but whole: none of the Brief's trimming ([pipeline.md](pipeline.md),
  section 6), so the references and the end of a long appendix are there to read.
- **Pages.** A PDF page as an image, named by its number or by a caption ("Figure 3"), for figures and tables the
  LaTeX text does not show. It works without the AGPL `pdf` extra, as a PDF read already does.

A paper the archive has not fetched is asked for at urgent priority, and the answer waits a bounded time; past it,
the agent is told the paper is being fetched and to ask again, rather than hanging. A paper the archive cannot get
says so.

The server's instructions tell agents to use it before the web for arXiv papers.

## Access

- **One endpoint on litwatch's host** (decision 86): streamable HTTP at `https://litwatch.andrei-homelab.com/mcp`,
  behind the gateway's existing virtual host, so it is reachable from the LAN and the tailnet and nowhere else. The
  gateway gains a `location /mcp` that proxies a stream (a homelab-config change).
- **Its own token**, a bearer token apart from the web app's `LITWATCH_API_TOKEN`: it sits in agent configurations on
  several machines, so revoking it must not sign the web app out, and when writes come they get their own.
- **Slow reads stay out of the web app.** A PDF read or a page render must not slow the web app's requests; whether
  that is a process of its own is the build plan's call.

## Clients

`~/dotfiles` carries the client side: the server in the Claude Code plugin and in Codex's `config.toml`, the token
read from `~/secrets/.env` through `agent-secret`. A new machine gets it with the dotfiles and the token.

## Not now

- **Writes**: reporting a Miss, saving a paper, reacting, asking for a Deep card (which spends on models). Added once
  use shows they are wanted, behind their own token.
- **Semantic search**: the archive's, later ([archive.md](archive.md)); agents rephrase lexical queries well enough to
  start.
- A "papers I opened" filter, cropped figures, access from outside the LAN and tailnet (CHPC), sources other than
  arXiv.
