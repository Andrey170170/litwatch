# litwatch v1 web app design

The second pass of the v1 web app design, agreed with the owner on 2026-10-13 in seven sketch rounds. The drawings are
the live sketches in `docs/specs/v1/sketches/` (`index.html` lists each round, what it asked and what was chosen; the last
round, `r7-settings.html`, is the whole app as chosen, Settings included, except the Digest's Week and History, which
are in `r2d-digest.html`). This file is the part a builder needs without opening them:
the principles, the shell, the pages, the interactions, the look's tokens and the anatomy of each repeated element.
Where this file and the spec (`docs/specs/v1/`) differ, the spec wins. Terms are the glossary's (`GLOSSARY.md`).

It replaces the first v1 design (this file and `docs/v1/design.html` before 2026-10-13, both in git history), which was a newspaper:
editorial and hard to scan.

## Principles

| Principle | What it means and rules out |
| --- | --- |
| **Colour before text.** | Rarity is a colour on every paper and digest; a topic is a badge in its own colour. A list is read by colour first, then by title. No grade words on rows. |
| **Positions stay put.** | Topics keep one order wherever they are listed (the sidebar, cards, digest groups, calendar slots: Focus, Area, Field, each in the owner's order), and a topic with nothing new keeps its slot. The badges on a paper row are the exception: the topic giving its highest level comes first (spec, `pipeline.md`). Controls sit in the same place on every list page. Nothing reorders under the reader. |
| **One rung at a time.** | Every page is built on the detail ladder (Mark, Gist, Brief, Deep card, the paper): each item opens one level deeper, and each level is enough to decide whether to open the next. |
| **Dense and scannable.** | Like 0.14's tables, not a newspaper: one line a paper where a list is browsed, prose only where a digest or a Brief is read. |
| **The page is the acknowledgement.** | An action shows its result where it was taken (a lit star, a lit thumb with its reason, a label). A toast appears only for results the page cannot show. |
| **Offered, never asked.** | Feedback is always at hand and never requested; Calibrate, behind the gear, is the one place that asks. No queues, no "mark as read". |
| **Kitchen behind a door.** | Overview, Needs you, the topic editor, runs, integrations and configuration are Settings pages; on the front are only the run status and a paper's own open states. |
| **Self-contained.** | With no integration connected every page works; only the buttons that need one (Deep read without a provider) are missing. |
| **Phone first, desktop whole.** | The phone keeps its controls at the bottom within reach of the thumb and moves by swipes; the desktop uses its width for a sidebar, a rail and full tables. |

## Shell

**Desktop.** A sidebar 236 px wide, then the main column (content at most 860 px wide; the shelves, tables and the
calendar take the full width). The sidebar, top to bottom: the wordmark with the run status; Digest, Topics, Papers;
the section "Following" with every followed topic in the fixed order (badge, name, and today's count where the topic
has a digest today, the count highlighted for Focus); Settings at the foot. A digest opened from the Digest tab slides
over from the right across the main column (the shade, Esc or "‹ Close" closes it). Everything else opens as a page
with "‹ Back" at its top.

**Phone.** A header, the page, and on most pages a bottom bar.
- The header on a top-level screen: ☰ and the wordmark (or the page's name), then the run status and ⚙ at the right.
  On a deeper screen: ‹ and the page's title.
- The drawer slides from the left, opened by ☰ or a swipe from the left edge on a top-level screen. The edge is the
  first 48 px: Android's gesture navigation owns about the first 24 dp and passes on a finger that landed just inside
  them, so half the zone is where such a finger lands and half is edge the page can reach. The drawer holds the
  sidebar's items, with Day, Week and History under Digest. It follows the finger and settles open or shut by where
  the finger let go and how fast, and a drag on it or on its shade shuts it; a drag that starts vertical scrolls
  instead.
- On a deeper screen the same edge swipe goes back, iOS style: the page follows the finger with the previous one
  under it. Every step is a history entry, so the system back gesture walks the same steps; a Calibrate session's
  cards are the one exception, as its arrows are: the session is one entry, and Back leaves it.
- A horizontal swipe away from the edge steps the page where there is a sequence: a Day or Week to the older or newer
  one, a paper to the previous or next in the list it was opened from. The content follows the finger; past a quarter
  of the width, or flicked, the next one comes in; where nothing lies the drag resists.
- Every control has a hit area of at least 40 px. The app is an installable PWA; the Android share target lands on
  the miss lookup (spec, first v1 decision 4).

**Run status**, in the wordmark on both: the papers read last night when the run went well ("530", a cyan square);
the progress while it runs ("212/530", amber, pulsing); the problem when something failed ("2 failed", red); the spend when a provider's month is projected over its budget
("530 · $ over", amber, steady). Failed outranks running, running outranks over budget. It opens Settings: Needs you
when something failed, Runs and cost when over budget, Overview otherwise. What counts as a problem is the spec's.

## Pages

### Digest

Three views: **Day**, **Week** and **History**. On the desktop they are a segmented control at the top of the tab; on
the phone they are the drawer's items under Digest, and the bottom bar holds the view's controls. The tab opens on
Day, today.

**Day.** The date with "‹ ›" to step (on the phone the bottom bar's "‹ Tue 13 Oct ›" with large arrows, plus the
swipe), "Today" over it on today. Then one digest card per daily topic in the fixed order, at Gist level (anatomy
below). On a Monday one line above the cards, "Weekly digests for 5–11 Oct are out ›", opens that Week; the weekly
cards themselves are not repeated in the Day. A weekend or holiday with no release
is one line, "No papers released", and stepping skips it.

**Week.** One week's weekly digests, every topic in the fixed order, the same cards; "‹ 5–11 Oct ›" steps by week.

**History.** Two forms, List and Calendar (the bottom bar's two buttons on the phone). Both load older weeks as they
scroll until the archive's start, where they say "The archive starts on Mon 1 Jun".
- *List*: the current week's weekly digests pinned at the top until the next Monday; then days, newest first, each a
  date header and one Mark row per daily topic; each week's weekly Marks close the week they cover; a weekend is one
  line. A date opens its Day, a row opens its digest.
- *Calendar*: a month as a grid, one row per week, Monday to Friday, then a W column for the week's weeklies. A day
  cell holds one bar per daily topic in the fixed order, coloured by the digest's best rarity (empty for nothing new,
  red for a digest that failed). The W cell holds the weeklies in two columns, Focus and Area level with the days'
  bars and Field beside them, so Field topics need no rows of their own. On the desktop each bar carries its badge
  and count and opens its digest, a date opens its Day and a W its Week. On the phone the bars are colour only and a
  cell opens its Day or Week. Today's cell is outlined.

A Day or Week opened from History is a page with the same bottom bar on the phone, so ‹ › and the swipe keep stepping
there.

**A digest** (the slide-over on the desktop; on the phone a page with ‹ back and no bottom bar, which does not step):
a large badge, the topic's name, "Daily digest · Tue 13 Oct" or "Weekly digest · 5–11 Oct" and the count line in the
rarity colour; the full text; then its papers by role as rows with Gists. A Focus daily digest: Central, then
Related, whose rows are one line each without a Gist. An Area daily digest: Picks, Mentioned (papers its text names), Also due. A weekly digest: "Central this
week" for Focus, Picks for the others. A digest with nothing due says so in one line; a failed one says it could not
be written and that the next run writes it again.

### Topics

**The Topics tab.** Cards grouped Focus, Area, Field in the fixed order, in a grid on the desktop. A card: the large
badge, the name, "312 papers · 41 saved", a thin bar of the topic's papers by rarity (the five levels as segments
sized by count), the title of its newest must read (which opens that paper), and its digest strip.

**A topic page.**
- The head: the large badge; the depth and cadence ("focus · daily and weekly digests") over the name; the subject as
  one line; the **digest strip**, the topic's recent digests as squares in their best rarity, newest at the right (the
  last 15 daily digests for Focus and Area, the last 8 weekly for Field), each opening its digest. Where the strip is
  too narrow, whole squares of the oldest drop out; a square is never cut.
- The controls in one row: search within the topic, ★ Saved, ⟨/⟩ Code, the **rarity bar** inline, the count, and the
  sort, Newest or Rarest (Best match while a search is typed).
- The papers as **shelves** by the topic's grade: Must read (epic and legendary) and Relevant (rare) open, Adjacent
  and "In the topic, not graded up" folded to a count until opened. Must read shows reading rows (title and Gist), the
  others table rows. The sort applies inside each shelf; a shelf loads more as it scrolls.

**Papers.** The whole collection with the same controls and shelves, plus: a wide search box over the collection and
the archive ("Search the collection and the archive (3.1M papers)"); a row of topic badges that filter to one or more
topics; Saved labelled "★ Saved · the library". The shelves are by mixed rarity: Epic and legendary, Rare, Uncommon,
Common. Rows carry their topic badges. While a search is typed, the archive's hits sit in their own band under the
collection, "Outside the collection · from the archive", each with its authors, year and id and a Process button.

**On the phone**, both pages put the search box and a filter button (with the number of active filters) in the bottom
bar. The button opens a sheet with the topic badges (Papers), the rarity bar, Saved and Code, the sort and the count,
and Done. The header shows the topic's badge and name, cut with "…" when long.

### Paper

**The header**, on a ground carrying the paper's rarity stripe and glow: the rarity's name ("Legendary") and the
topic badges; the title; the authors (the first three and "+3", which expands); the facts line: the arXiv id (a link,
with ⧉ to copy it), the date, the version ("v2 of 2"), the kind (Analysis, Method…), the published venue when DBLP
knows one, and "⟨/⟩ code ↗" and "◆ weights ↗" when released. A paper outside the collection says "Not in the
collection" in place of a rarity.

**The ladder**, in the reading column, each rung with its name in a narrow side column:
1. **Gist**, with ✓ and ✗ to say whether it is accurate.
2. **Brief**, with ✓ ✗. A Brief that found the abstract overstating says so first, in a red-edged callout. A Brief
   from an older revision says which, and that the current one waits for the text. Where a paper has no Brief, the
   rung is **Abstract**, with one quiet line saying why there is none.
3. **Deep card**: its claim in bold, then each section as a rung of its own under it: Summary, Setup (models and
   tasks as tags, and how far it transfers), the topic's own facts and text (for CoT faithfulness: mismatch, ground
   truth, monitor, signals, then detection, definition, generality), Limitations, Prior work (rows that open the paper
   when it has an arXiv id), Among the collection (rows with their rarity stripe and a tag: agrees, extends,
   contradicts, supersedes). It ends with what it was written from and when, and "Write again". Without a card: one
   line saying when cards are automatic and "Deep read" to ask for one; while one is queued or being written, a
   status line with a pulsing dot.
4. **The paper**: links (arXiv page, PDF, LaTeX source), the versions, the published version or "Preprint only, as
   far as DBLP knows" with "Paste a DOI", and its classes with their parents.

**The rail** (desktop), beside the reading column:
- The Gist, once the reading column's Gist has scrolled out of view; it moves in, and back out on scrolling up.
- One row per topic the paper is in: badge, name, grade ("must read · landmark") and the verdict ("Fit central ·
  Weight landmark", or "your label: related"), the two thumbs, and Label, which opens the **Fit control**.
- The quick actions as a 2×2 grid: Save, BibTeX ⧉, arXiv ↗, PDF ↗, with Deep read across the full width while the
  paper has no card. For a paper outside the collection: Process, BibTeX, Deep read, Report as a miss. Once
  fetched, Process reads "Fetched", and "Fetched · next run" while the next run will process it (not dismissed).

**On the phone** the page is one column: the header and the ladder without its last rung. The bottom bar: ☆ Save, 👍,
the paper's place in its list ("3/22"), 👎, and ⋯. ⋯ opens the paper sheet: a label per topic (the topic's name, its
grade and whose Fit it shows, then the Fit control), the actions and links as the same grid (arXiv, PDF, LaTeX, Code,
Weights, BibTeX), then versions, published version and classes, and Done. A swipe steps to the previous or next paper
of the list it was opened from.

A paper outside the collection on the phone: the header and the Abstract (no Gist or Brief), and a bottom bar of
Process, Deep read (where a provider is configured) and ⋯; its sheet has no labels and adds "Report as a miss".

**Open states.** A paper with an item on Needs you (below) shows it on its own page, in the same colours and with the
same actions. A note sits under the header: yellow for a split or escalation ("Split on CoT monitoring. Judge central,
check adjacent; central stands until it is labelled."), red for ungraded, a failed analysis or full-text pass or
awaiting classification, each with its buttons. A paper whose analysis failed has no Gist, Weight or Brief until it
succeeds: in lists it shows its title alone, on its page the abstract stands in. In the rail, a split or escalated topic's row takes a yellow edge, names its state, the two
opinions and the Fit that stands ("Split · judge central · check adjacent · central stands", "Escalated · judge related
· check adjacent · adjacent stands") and keeps its Fit control open until labelled; an ungraded topic's row
says "ungraded" in red with "Grade it now", then "Grading again" once asked. On the phone the note's Label opens the
paper sheet, where the topic is marked the same way. A paper awaiting classification says so in red in place of a
rarity. Settling an item on either page settles it on both; on the paper's page the note goes, so a toast says what
was done. List rows carry the state mark (anatomy below).

### Settings

Behind the gear: the sidebar's foot on the desktop, the header's ⚙ and the drawer on the phone. The pages and what
they hold are the spec's (`feedback.md`, `pipeline.md` sections 9 and 10, `models.md`, `integrations.md`, `data.md`);
the spec's Quality page is two pages here, Overview and Needs you.

**Around the pages.** On the desktop Settings takes the sidebar: the wordmark, "‹ Back to reading", "⚙ Settings", then
the pages in three groups, Reading (Overview, Needs you, Calibrate, Misses), Following (Topics, Taxonomy) and System
(Runs and cost, Integrations, Configuration), with a count beside a page that wants something. The gear opens
Overview. A page inside a page (a topic's editor, Jobs, an integration's events, a past Calibrate session) has
"‹ Back"; a paper opened from Settings keeps the Settings sidebar. On the phone the gear opens the list of pages in the
same groups, each with a line under its name ("6 waiting", "Last session 6 Oct"), and every page is a deeper screen.
Page heads stack on the phone, and wide tables drop columns rather than scroll sideways. Under the pages, in the
desktop sidebar's foot and at the end of the phone's list, Appearance: System, Light or Dark, System by default. The
choice is kept on the device and put on the page, and on the browser's bar, before its first paint.

**Colours.** An item carries a 3 px left edge: yellow (`--acc`) asks for the owner's word, red (`--sig`) reports a
failure, cyan (`--cy`) is fine or settled, amber (`--warn`) is a projection over budget or a difference worth a look.
A settled item turns cyan, keeps its result in place and fades until the page is left. Shares are bars with their
number, cyan, amber or red by how good they are.

**Overview.** Four tiles, each opening its page: what needs the owner (the count, yellow), last night (date, papers,
digests, cost; red when it failed), this month's spend against the budgets (amber when a provider is projected over),
the last Calibrate session. Then the spec's numbers: recall per Focus topic (classification, membership, grading, end
to end, n; "Run replay"), precision at relevant (with "below, owner wanted it"), reactions over 30 days per topic and
stage, and "Matters, no topic".

**Needs you.** Only what waits on the owner, grouped by kind, each group with one line on what it means: Focus splits
and Area escalations (yellow, settled with the Fit control, the standing Fit underlined); awaiting classification
(Dismiss, Classify by hand, Fix the text), ungraded (Grade it now), analysis (the error, Try again), full text (Try
again, Use the PDF) and digests (Write it again), red. A title opens its paper, where the same item shows (Paper, open states).

**Calibrate.** A first page with "Start a session" and the past sessions (date, answered, seed, how many answers differ
from the pipeline). A session draws its papers as the spec says (about 20 per Focus topic, half at relevant or
above and half below, with a recorded seed): a strip of one square per paper (answered cyan, the current one yellow) that jumps to any paper;
the paper, blind (title, authors, id, date, Gist, Brief or abstract, classes; no grade, rarity or topic marks); the
spec's questions as segmented controls; "‹ Back", "3 of 20", "Next ›". On the phone the strip sticks under the header,
the bottom bar holds ‹ Back, "3/20 ▴" and Next ›, and "3/20" opens a sheet listing the session's papers to jump to. A
past session opens its review: each paper with the owner's Fit against the pipeline's, amber where they differ; a row
opens its paper.

**Misses.** Each report with its topic, date and the stage that let it go, and its trace: what each stage concluded
for the owner's topic when the reported paper went through the pipeline, in the miss record's cells: classifier,
uncertain band, keywords, then one per grader the topic's depth asks (Focus: judge, check; Area: judge, area check,
check; Field: field grade). A cell's top edge is cyan where the stage would have admitted it, red where it would have
let it go, grey while the run is pending or the grader was not asked; grading always answers, since it runs for the
reported topic whatever classification said, and "the stage that let it go" reads the Fit that stands by the depth's
rule, which the API carries. Edit the topic, Edit the class, Resolved.

**Topics and the topic editor.** Focus, Area and Field lists in the fixed order. A row drags by its ⋮⋮ grip to a new
place within its depth, which becomes the order of the sidebar and the Digest; the rest of the row opens the topic's
editor: class, depth, subject, rubric, interests, keywords (with their matches over 90 days), look (badge, monogram,
colour) and status. Save does nothing until something has changed.

**The re-run offer.** Saving an edit puts the offer in place of Save and Cancel, in the note's style. Its first line says what the save changed: an edit to what the models
read applies forward ("Saved as version 6. It applies to papers classified or graded from now on."); following a
different class, or moving, splitting or merging classes, changes at once how existing memberships resolve, and the
line says so ("Saved. Topic pages and rarity follow the new shape now; papers it brings in show ungraded until a
re-run grades them."). Then the spec's scoped re-runs as rows to tick, each
with its paper count and estimated cost, a ceiling ("Stop at $5.00"), then Not now and "Run · 476 papers ≈ $0.67".
Run starts it and the toast says where its progress is. Not now keeps the offer in Jobs as "not run", and the toast
says so; nothing runs by itself.

**Taxonomy.** The tree as rows: a caret, the name, the badge of the topic following it, members, threshold. The caret
folds and unfolds a branch; the rest of the row opens the class's editor in place under it (name, description,
threshold) with the same Save and offer. Export YAML and Import YAML in the head. On the phone the members column is
hidden and long names are cut with "…".

**Runs and cost.** Spend per provider as a bar: month to date solid, projected to the month's end lighter, the budget a
tick, amber when projected over. Under them, Deep cards' month to date as a subscription estimate, with no budget
and never amber, and "at least" with the count of attempts whose usage is unknown when there are any. Then the nights as a table (night, papers, graded, digests, took, cost, and what
failed, red-edged), then the latest three jobs and "All jobs ›". **Jobs** holds the work outside the nightly run in
four groups: Not run (yellow; Run now, With tonight's run, Discard), Running and queued (cyan, with progress), Failed
(red; Try again) and Done. A job that is also a Needs you item settles with it.

**Integrations.** A card per integration: its name, what it is connected to, what it does and its status (cyan up to
date, red failed, grey not loaded); a failed event with Retry now and Skip this event; All events. The outbox counts
under the cards. **An integration's events**: time, event, paper and result, newest first, filtered by all, failed or
waiting; a failed event has Retry and Skip.

**Configuration.** The models per role and `litwatch.toml` as loaded, secrets masked, read-only.

## Interactions

**Reacting.** A thumb, on a topic's row in the rail or in the phone's bottom bar, opens its reasons for that topic
(spec, `feedback.md`): down: Wrong topic, Overrated, Not interesting, Already knew it; up: Underrated, Great find. On
the desktop they open as a row of chips under the topic's row; on the phone as a sheet, with a topic choice first when
the paper is in more than one. Picking a reason records the reaction, and the thumb stays lit with the reason beside
it. Opening the reasons of a lit thumb also offers Clear. Nothing is recorded until a reason is picked.

**Acknowledgement.** The result shows in place, so most actions say nothing more. A toast appears only for what the
page cannot show: a copy (BibTeX, the arXiv id), Process, a miss report, and a reaction that writes the owner's word
on a Focus topic, together with taking such a reaction back ("Marked not on CoT faithfulness; it leaves the library",
"Mark on CoT faithfulness taken back"). The toast is short, on the surface colour with a 1 px yellow edge and a cut
corner, centred under the main column on the desktop and just above the bottom bar on the phone, and gone after about
2.5 s.

**Hover** (desktop): on a paper or digest row, the rarity glow doubles in strength and grows half again as long, and
the stripe widens from 4 to 6 px, eased over 160 ms. Buttons fill yellow. The wordmark glitches once (a red and cyan
split, 450 ms).

**Press**: rows take the hover colour while held; buttons and chips also sink by 1 px.

**Motion**, short and plain, all of it off under the system's reduced-motion setting:
- going deeper or back: the page slides in 28 px from the side it comes from, fading in, 220–240 ms; a top-level
  switch on the desktop fades in, 160 ms;
- the slide-over enters from the right, 260 ms; opening or closing it leaves the page under it still;
- a phone sheet rises from the bottom, 240 ms, and its shade fades in;
- a step (day, week, paper) slides 40 px, 200 ms, after the finger;
- the curve throughout: `cubic-bezier(.2, .85, .25, 1)`.

**Sheets** (phone): closed by the shade, by their button, or by dragging them down by the handle at the top. Past a
third of the sheet's height, or flicked, the sheet closes; otherwise it springs back.

## Look: Night City, toned down

After Cyberpunk 2077's interface: a dark ground, one hard yellow, red for section names and failures, cyan for the
system, the five rarity colours as the only other colour. Toned down: no scanlines, plain numbers, the glitch only on
the wordmark. Dark first; Light is a full equal, following the system preference.

### Tokens

| Token | Light | Dark | Used for |
| --- | --- | --- | --- |
| `--bg` | `#e9ebe7` | `#07090d` | page |
| `--surface` | `#f6f7f3` | `#0c1117` | sidebar, cards, lists, bars, sheets |
| `--fg` | `#0c0f13` | `#dde6ec` | text |
| `--fg2` | `#47515b` | `#8a9eac` | secondary text, Gists in rows |
| `--fg3` | `#7b858e` | `#546676` | meta, counts, quiet lines |
| `--line` | `#cdd2d1` | `#1a2833` | borders |
| `--line2` | `#dfe3e1` | `#121c25` | rules between rows |
| `--hover` | `#f7f1b8` | `#0e2027` | hover and press |
| `--sel` | `#f3e600` | `rgba(243, 230, 0, .1)` | the selected nav item (its text `#0c0f13` in Light, `#f3e600` in Dark) |
| `--acc` | `#f3e600` | `#f3e600` | the one accent: selection, hover fill, the toast's edge, the phone header's rule, what asks for the owner's word |
| `--sig` | `#d90f2d` | `#ff3b4e` | section names, failures, the Focus count, the overstatement callout |
| `--cy` | `#00899b` | `#2de2e6` | the run status when all went well, what is fine or settled |
| `--warn` | `#d2860b` | `#e8b23d` | the run status while running, a projection over budget |

**Rarity**, for the stripe, the glow, the shelf heads, the bars and squares:

| Level | Light | Dark |
| --- | --- | --- |
| Common | `#7c858d` | `#8a96a0` |
| Uncommon | `#179d48` | `#36cf78` |
| Rare | `#1d6fe8` | `#3d8dff` |
| Epic | `#9b3ff2` | `#b45cff` |
| Legendary | `#ef8700` | `#ff9a1f` |

**Faces.** Chakra Petch for everything that is read, including Gists, Briefs, digests and Deep cards (no serif).
Rajdhani 600–700 for names and labels: page titles, the wordmark, nav items (the three tabs in caps, letter-spaced),
digest card topic names, section names, buttons, the date strip, rung names. JetBrains Mono for numbers and ids:
counts, dates on rows, arXiv ids, the run status.

### Type scale (desktop; the phone the same unless given)

| Element | Face | Size | Weight |
| --- | --- | --- | --- |
| Wordmark | Rajdhani, caps, .2em | 18 | 700 |
| Page title | Rajdhani | 25 (phone paper header 21) | 700 |
| Sidebar item | Rajdhani | 15 (drawer 14) | 600 |
| Section name | Rajdhani, caps, .22em, `--sig`, "// " before | 11 | 700 |
| Digest card topic name | Rajdhani, caps, .07em | 15.5 | 700 |
| Digest card count | Mono, caps, in the rarity colour | 11.5 | 400 |
| Digest Gist | Chakra Petch | 13.5 | 400 |
| Paper row title / its Gist | Chakra Petch | 13.5 / 13 | 500 / 400 |
| Table row title | Chakra Petch | 13.5 (phone 14) | 500 |
| Reading row title / meta | Chakra Petch | 14 / 11.5 | 500 / 400 |
| Topic subject | Chakra Petch | 13.5 | 400 |
| Paper authors / facts | Chakra Petch, Mono for the id | 13 / 12 | 400 |
| Paper Gist | Chakra Petch | 16 | 400 |
| Brief, digest text, Deep card prose | Chakra Petch | 14.5 | 400 |
| Deep card claim | Chakra Petch | 15.5 | 600 |
| Rail Gist | Chakra Petch | 14 | 400 |
| Toast | Chakra Petch | 12.5 | 400 |

### Night City detail

- Cut corners: lists, digest cards and buttons have the top-right and bottom-left corners cut at 9 px; segmented
  controls and the toast have the top right cut at 8 px, badges at 5 px. No rounded corners.
- Section names in `--sig` with "// " before them.
- The selected nav item: the `--sel` ground with an inset 3 px bar at its left (`--fg` in Light, `--acc` in Dark).
- The phone header has a 2 px `--acc` rule under it; in Dark the drawer has a 1 px `--acc` edge.
- The run status's square glows faintly in Dark.

### Anatomy

**Topic badge.** The topic's two-letter monogram in a square of its colour, the depth by shape: Focus filled (white
letters), Area tinted (the colour at 18 % with the letters in it), Field outlined (1.5 px). 20 px; 28 px large (heads,
cards); 16 px inside rows. Each topic gets a saturated hue at creation, changeable in its editor (the sketches use
`#c026d3`, `#e11d48`, `#0891b2` for Focus, `#4f46e5`, `#ea580c` for Area, `#0284c7`, `#65a30d`, `#ca8a04`, `#0d9488`
for Field). Coloured everywhere, rows included.

**Rarity on a row.** A 4 px stripe of the rarity colour at the row's left edge, full height, plus a glow: the rarity
colour fading to transparent across the row, `linear-gradient(90deg, color-mix(in srgb, <rarity> S, transparent),
transparent min(60%, L))`. Strength S and length L by place: Digest rows (paper rows and Mark rows) 20 % over 220 px;
topic page and Papers rows 15 % over 220 px; the paper header 15 % over 132 px; the rail's Gist none. Light mode takes
three quarters of each strength. A quiet row (nothing new) has no stripe or glow.

**Digest card** (Gist level): the topic's badge and name, the count line at the right in the colour of the digest's
best rarity ("2 new · 1 must read", "11 new · 1 pick", "Week · 3 picks"); the Gist; up to three picks as paper rows
(stripe, glow, title, date; no badges, the card names the topic); "+10 papers" when more are due. The whole card opens
the digest. A quiet topic is the head alone, "Nothing new", in its slot; a failed one is one red line. On the phone the
count sits on its own line under the name, so it starts at the same place in every card.

**Mark row** (History): the badge, the topic's name, the count at the right, with the stripe and glow.

**Paper row**, three forms: the digest row (title and date); the reading row (title and ★ on the first line, the Gist,
then badges where the list mixes topics, kind, ⟨/⟩ and ◆ facets and the date); the table row, one line (stripe, title,
badges on Papers, kind, facets, date, ★), its stripe an inset full-height edge. ★ saves and unsaves in place.

**State mark.** A paper row whose paper has an open state (Paper, open states) carries an 11 px triangle in its
top-right corner: yellow for the owner's word, red for a failure; yellow when it has both. A topic's states count in
that topic's lists (in any topic on Papers); a paper's own states (analysis, full text, awaiting classification) count
everywhere.

**Rarity bar.** Five segments, Legendary to Common, each in its colour (an 18 % ground and a 3 px top edge), sized by
how many papers the other filters leave at that level (with a minimum, so every level stays tappable), each showing
its count and name. A tap filters to that level; several can be on, and the rest dim.

**Shelf head.** A band of the rarity's colour, 40 px tall with air above and below, carrying the rarity square, the
shelf's name and its count: the colour is found before the name is read. A folded shelf adds "show", an open lower
shelf "fold".

**Digest strip.** Squares of 14 px (11 px on Topics cards), 3 px apart, in the digest's best rarity; nothing new is an
empty square, a failed digest a red ×. Newest at the right, after the label "Daily digests" or "Weekly digests".

**Fit control.** Five equal segments: central, related, adjacent, none, unsure. The owner's label is filled; with no
label, the verdict's segment is underlined. Tapping the filled segment removes the label. Unsure counts nowhere.

**Thumbs.** Two buttons, grey and quiet until used; a lit thumb takes the accent edge and shows its reason. Reasons are
chips; the chosen one has the accent edge.

**Action grid.** Equal cells in two columns, in Rajdhani caps; Save fills yellow when saved ("★ Saved"); an external
link ends in ↗, a copy in ⧉.

**Phone bottom bar.** On the surface colour, above the safe area: the page's controls in 46 px cells (a paper: Save,
the thumbs, its place, ⋯; a Day: ‹ date ›; a list: search and filters).

**Sheet.** From the bottom, on the surface colour: a handle at the top, section names in Rajdhani caps, chips and
segmented controls, and a full-width Done outlined in the accent.
