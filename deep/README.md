# litwatch-deep

The deep service: an agent with one JavaScript REPL tool reads a paper and writes its Deep read
([`docs/specs/deep-read.md`](../docs/specs/deep-read.md)), a reviewer and a blind reader on the same model score
the read, and the writer revises it at most twice. litwatch's `deep` integration submits papers to it (`POST /process`) and polls for
the result (`GET /jobs/<id>`); what a finished job carries is in [`docs/integrations.md`](../docs/integrations.md).
The service writes reads only. A Deep card from before the read stays as litwatch stored it.

A submission may carry an `Idempotency-Key` header (litwatch sends its attempt): a key the
service still has is answered `200` with that key's job instead of starting another, so a
submission sent again after a lost reply never runs twice; `GET /keys/<key>` finds that job
(404 when the service has none). A failed job (`status: "error"`)
says whose failure it was in `failure`: `service` when the model call failed (the sign-in, a
usage limit, the provider) or the job could not start, `paper` when no valid read was submitted
within the budget or the content filter stopped the model. Jobs and keys live in memory: a
restart forgets them.

## A run

The writer's first message carries the paper's outline and its main text; the bibliography and the appendix stay in
the session (`paper`), wherever they sit in the text, to be read where the read needs them. The headings are found
once per run (`src/paper.ts`): the session's `sections()` gives them, the first message's outline lists them, and
the checks look sections up in them. A LaTeX source's carry their numbers (`3.2`, `A.1` from `\appendix` on); a
PDF's text is read in the two forms litwatch's archive writes, Markdown with bold headings (`## **References**`)
and plain text, where only two lines are told as headings: the References line, and the appendix's first, which
is a line that names it (`Appendix`, `Appendix A: Proofs`) anywhere past the Introduction, or a lettered line
(`A Additional samples`) after the references.
Besides the paper's text (`sections()`, `section()`, `find()`, `refs()`), litwatch's papers (`store.search`),
Semantic Scholar (`s2.lookup`) and the open web (`fetch`), the session has:

- `figures()`, `figure(file, name?, number?)`: the figure files of the paper's e-print, fetched from arXiv once per
  run at the version litwatch's text is of (`paper.body_version`), and one of them kept as a figure of the read.
  `number` says which of the paper's figures it is (`"Figure 6"`, `"Figure A.1"`). PDF, PNG, JPEG and GIF files
  convert; EPS does not.
- `pdfPage(n)`, `crop(n, [left, top, right, bottom], name, number?)`: a page of the paper's PDF, and a part of one
  kept as a figure: for a paper with no source, an EPS figure, or a figure in several files. The PDF is litwatch's
  (`GET /api/papers/<id>/pdf`, with the read token); where litwatch has none and answers with a redirect, it is
  arXiv's at the version of litwatch's text (`arxiv.org/pdf/<id>v<version>`), not the latest the redirect points
  at. The version of the PDF litwatch's own archive holds is litwatch's to match to the text.

With no version from litwatch the e-print and arXiv's PDF are the latest, and the read carries a process note
saying so. Each figure made is one record (`src/figures.ts`): its WebP, the view the model saw, and its number.

- `render(read, target?, width?)`: the draft drawn by the web app's own components on litwatch's render page
  (`<LITWATCH_URL>/render.html`), whole, one section or one block, at the desktop's width or the phone's. It draws
  only the figures this session made.
- `check(read, standBy?)`, `submit(read, standBy?)`: the checks below, without and with submitting. `standBy` lists
  refused places and numbers the writer has checked, each as the refusal quoted it ("Table 2", "0.00005"): each is
  then not refused but sent to the reviewer by name, and the trace keeps it on the submit. Structural errors, KaTeX
  and figures the session did not make cannot be stood by.

Every image a function makes goes back to the model with the output of the cell that called it; the model keeps the
images of its latest cells, and older ones are replaced by a line. A figure is stored as WebP, at most 1600 px wide,
under the name the read cites it by; a run makes at most 60, 24 MiB together, within what litwatch takes of a job.

`submit` takes the read's schema (`src/read.ts`) in a draft's form: the model's own process notes only, and a
diagram's coordinates optional. It then checks in code (`src/checks.ts`) what a model need not judge. A check
refuses only on evidence: a place a LaTeX source's own numbering rules out, a number the paper's text lacks. What
code cannot settle either way is neither passed in silence nor refused; it goes to the reviewer by name.

- the sections in their order, one lens per topic the read is written for and no other, ids unique, each table row
  with as many cells as the table has columns, each edge between nodes the diagram has, a placed node at least 40 px
  tall;
- every `fig` block naming a figure this session made, under the number it was made as, and every figure and table
  cited, a `fig` block's number included however it is written (`Figure 6`, `6`). A cite is read in any case
  (`fig. 1`, `Table 2`), a range names every label in it (`Figures 1–3` cites 2 as well), and a label is read
  whole (`3`, `A.1`, `S2`), by one parser for every lookup (`src/places.ts`). A LaTeX source's floats are counted:
  where LaTeX's defaults number them, a number past the count is refused; a source with no table has none,
  however it numbers;
- every `where` (a claim's verdict, a CARD note, a diagram's part) naming a place: a section or an appendix by its
  number, a figure or table, a numbered equation, algorithm, theorem, footnote, page or line, or a section by its
  title, whole at any length (`PPO`) or in part where the part is long enough not to match by chance. A LaTeX
  source whose sections LaTeX's defaults number carries its outline, so a section number it lacks is refused
  (`§2.3` needs section 2 to have a third subsection), and so is a `where` that names nothing of these;
- every equation and inline formula through KaTeX;
- every number given as the paper's being in the paper's text, in any common form (`src/numbers.ts`: 0.34 or 34%,
  .95 or 0.95, 1,200 or 1.2k, rounded to the read's precision): in prose, captions, facts and their tags, tables
  with their group headings, and diagrams. A value the text lacks passes when its sentence cites a figure this
  session has an image of, made with that number or shown in a `fig` block: it was read off a plot, and the reviewer
  is shown that image. A sentence, or a table's caption, that cites several figures needs the image of each, and
  so does a figure's own caption that cites another figure. Not checked: whole numbers under ten, years from 1950 to next year, the reading time, other papers' numbers in
  Related work, an equation's own TeX (notation, not values), a table caption's counts of the rows and columns the
  read kept (`11 of its 12 rows`), and the read's own labels (section titles, `h3`, fact names). The reviewer's
  prompt names these, so it checks them itself.

What goes to the reviewer by name, since code can neither confirm it nor rule it out:

- any figure, table, section or appendix of a PDF's text that the text does not itself name or number on a line,
  and a `where` of such a paper that names nothing code knows;
- an equation, algorithm, theorem, footnote, page or line, which code does not look up;
- in a LaTeX source: a figure or table past the main text's count and within the source's whole count, an
  appendix's own labels (`A.1`, `S2`), an appendix where no command starts one, and a `fig` block's number with no
  number in it;
- every number of a kind a LaTeX source numbers itself (`ownNumbering` in `src/paper.ts`). The source's numbering
  is taken as LaTeX's defaults only where nothing in it says otherwise. A `\setcounter`, `\addtocounter`,
  `\numberwithin` or `\counterwithin` on the counter, any `\the…` of it, chapters or a class that has them, the
  `chngcntr` package, a `\ContinuedFloat` (for figures and tables), and `secnumdepth` or a class that numbers
  sections in Roman numerals (IEEEtran, REVTeX; for sections) each make every figure, table or section number of
  that paper unknown. Its headings then carry no numbers in `sections()` either.

A draft that passes is laid out: a diagram without coordinates gets them from ELK's layered algorithm
(`src/layout.ts`), so the stored read always carries them.

Then the reviewer: one call, no session, with the paper's whole text, the read, the figures the read cites for plot
values, and the places code could not look up. The text is cut only where the model's context (its `contextWindow`)
has no room for it: the bibliography goes first, then the text's end; the reviewer is told what was left out, and
the read carries a `tool` note saying so. It answers through a `review` tool with four scores (faithfulness, clarity, depth, calibration, 1 to 5),
five checks (`anchors`, `experiments`, `why_boxes`, `voice`, `length`) and its findings. `length` is code's, not the
reviewer's: the read's text past 60% of the paper's main text fails it (`src/length.ts`). The writer, in `check()` and
`submit()`, is told the measure and the aim of a third, and the reviewer the limit as well; a writer told the limit
wrote up to it.

Beside the reviewer, in the same round, the blind reader (`src/blind.ts`): one call, no session, given the read as a
reader meets it (its text in reading order, a WHY box as its question and its folded answer, the figures it shows)
and never the paper. It answers through a `follow` tool with its retelling of the paper's argument, the places it
lost the thread and a followability score, which join the round's verdict; a blind reader that gives no verdict
leaves the round without one, as the reviewer's would. A read passes when every score, followability included, is
at least 4 and every check passed. Otherwise both judges' findings go into the writer's own conversation together
and it revises: the first write plus at most two revisions. A read that still fails is kept, with a `review` process note
saying what it failed on; a review that could not run leaves the read with a `tool` note saying it was not
reviewed. That holds for a revision too: the verdict on the draft before it is not carried over to it, and stays
in `reviews` as what happened. The job returns each round in `reviews`, and the figures the read shows in `figures`. Both the review
and the job take a read's figures as they were when `submit` accepted it: a figure made again under the same name
in a revision that is then not accepted does not change the read that is kept.

A job's `trace` (in `GET /jobs/<id>`) is what the run did, each event with `t`, milliseconds since it
started: the model's `assistant` turns (text, reasoning summary, stop reason, usage), `cell`s (code,
output, error, time, how many `images` came back, and `timedOut` on the one that ran past its limit, which ends
the session), `host` calls (name, arguments cut to 200 characters, time, `ok`, the characters returned, and a
failed call's `error`, cut to 300), `submit`s (with the errors of a rejected one), `review`s (the round, whether
it passed, its usage), and the `system` messages the service sent the writer: a nudge to submit, or a review's
findings. A `fetch` call adds `fetch`: each redirect it was answered with (`hops`, URL and status), then the `url`
and `status` that answered and the `bytes` of body read. The model is asked for detailed reasoning summaries.
litwatch keeps every attempt's trace and reads it with `litwatch deep trace`.

A read may carry `process_notes`, `[{kind, source, note, url?}]`: what the agent could not do because of its
tools, access, budget or input (`kind` access, tool, budget or input), as against the limitations, the paper's own.
The model may give up to six (`source: "model"`, a note of at least 20 characters, a `url` only absolute http(s)),
validated with the read. The service adds its own (`source: "service"`) once the run is over, after `submit`'s
validation, so the model can neither forge nor drop them, and what happened after the submit counts too: one note
per kind of event, with how many times and the first URL concerned, for a fetch the network guard refused, a fetch
failed on the network (DNS, the connection, a timeout, a body cut off; never an HTTP status, which is an answer),
the fetch budget used up, a cell timeout, an image the browser or the render page was not there to make, and
figures taken without the text's version; and the review's notes above. litwatch shows them folded on the read and in Needs you's Deep
notes, and `litwatch deep notes` lists them; they go to neither Zotero nor Notion.

The read places the paper among litwatch's collection (`among_collection`): papers it agrees with,
contradicts, extends or supersedes. `submit` takes only ids a `store.search` in the same session
returned from the collection. `paper` is the archive's LaTeX source, or the PDF's text as markdown.

A read's lens names its topic by the topic's slug (`selections[].slug`), or by its name when the submission
carries no slug. The extensions a submission asks for (`faithfulness`, `monitoring`) add that topic's questions to
its lens; they are no longer objects of their own.

`npm run read-schema` exports the stored read's schema to `web/src/deepread/read.schema.json`, from which the web
app's types are generated (`pnpm generate:read` in `web/`); run both after changing `src/read.ts`.

## Running it

The images need poppler (`pdftoppm`, `pdfinfo`) and Chromium's headless shell at the revision the pinned
`playwright-core` drives; the image has both. On a development machine: poppler-utils from the system, and
`npx playwright-core install chromium-headless-shell`. The tests that use them skip where they are missing, told
by what is on disk; where they are installed, a browser that does not start fails the tests. One of them draws a
read on the web app's own render page and skips until `pnpm build` in `web/` has built it. CI's deep job installs
all three and sets `DEEP_IMAGING_TESTS=required`, under which none of these tests skips: what is missing fails.

```sh
npm ci
npm test && npm run typecheck
node src/main.ts login    # sign in once; the auth file keeps the session
node src/main.ts serve
```

## Environment

| Variable | Default | |
| --- | --- | --- |
| `DEEP_PORT` | `8081` | Port to listen on |
| `DEEP_AUTH_FILE` | `/var/lib/litwatch-deep/auth.json` | Where `login` keeps the model session |
| `DEEP_MODEL`, `DEEP_EFFORT` | `gpt-6.1-sol`, `high` | The model and reasoning effort, the writer's and the reviewer's |
| `DEEP_MAX_CELLS` | `100` | REPL cells per paper, revisions included |
| `DEEP_CELL_TIMEOUT_S` | `90` | One cell's time limit |
| `DEEP_WALL_CLOCK_MIN` | `60` | One paper's time limit, its review and revisions included; keep it below litwatch's `[integrations.deep] timeout_minutes` (75), past which litwatch ends a running job as stuck and may start a duplicate |
| `DEEP_MAX_OUTPUT_CHARS` | `8000` | Output kept per cell |
| `DEEP_MAX_FETCH_CHARS` | `200000` | Text kept per `fetch` |
| `DEEP_MAX_FETCH_MB` | `2` | MiB read per `fetch`, fractions allowed; the rest of the body is never read |
| `DEEP_MAX_FETCHES` | `60` | `fetch` calls per paper; the next one fails in the cell |
| `LITWATCH_URL` | `http://litwatch:8080` | litwatch v1, for the agent's `store.search` (`GET /api/papers`), the paper's PDF (`GET /api/papers/<id>/pdf`) and the render page (`/render.html`) |
| `LITWATCH_TOKEN` | | litwatch's read token (`LITWATCH_READ_TOKEN` there), which may only search and read papers and fetch a paper's PDF; the render page takes none |
| `S2_API_KEY` | | Semantic Scholar key; without one `s2.lookup` is rate-limited |
| `DEEP_CONTACT` | | An address the fetched sites can write to, sent in the User-Agent (arXiv asks for one) |

A secret may also be given as a file: `LITWATCH_TOKEN_FILE`, `S2_API_KEY_FILE`.

The agent's `fetch` reaches any public site and never a private one (litwatch decision 80). It is a GET with
the service's own user-agent, no cookies, and no headers the agent sets. Every address a name resolves to must
be public unicast, checked where the socket connects, and a literal address in the URL is checked up front, so
loopback, the LAN, link-local (cloud metadata) and unique-local addresses are refused, and so are the IPv6
transition ranges that embed an IPv4 address (NAT64, 6to4, Teredo). An IPv4-mapped IPv6 address
(`::ffff:a.b.c.d`) is judged as the IPv4 address it carries. Counts must be whole numbers; a size or time may be fractional
and is rounded down to bytes or milliseconds. Each must come to at least one and stay within what its consumer
holds, or the service refuses to start. Redirects are followed by hand, at most five, each hop checked again. Each site is paced at one
request a second at most (arXiv and Semantic Scholar slower). anonymous.4open.science links are read through
its JSON API, since its pages are built in the browser. `store.search` and `s2.lookup` don't go through this
guard: litwatch itself is on a private address.

The paper's e-print comes from arXiv through the same guard, pacing and User-Agent, whole and in memory (at most
150 MiB); only its regular figure files are read, never written to disk under their own names. The paper's PDF
comes from litwatch, with the token; where litwatch answers with a redirect, the redirect is not followed and
arXiv's PDF of the text's version is fetched as any public file, without the token.

The browser (`src/imaging.ts`) has no business on the network beyond the render page. Every request to another
origin is refused; the service makes the allowed origin's requests itself and hands the page the answers, so a
redirect is never followed and is refused whole; names other than the render page's host do not resolve, and
service workers and WebSockets are off. Figures reach the page as data URLs, and a page is given three minutes at most. It also encodes the figures (WebP on a canvas), so it decodes images from
arXiv: it runs without Chromium's own sandbox, as Playwright starts it in a container, and the container is its
boundary.

`store.search` asked litwatch 0.x's `/papers/search` before v1; a 0.14 deployment keeps running
the image it was built with.
