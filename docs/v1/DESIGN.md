# litwatch v1 web app design

**Superseded (2026-10-13).** This is the first v1 design, which the built web app follows. The owner found it too
editorial and hard to scan; it is being redone in a second pass after the core redesign (`docs/v1/spec/`). Keep it as a
record of what was built until that pass replaces it.

Agreed with the owner on 2026-10-11. The drawings are `docs/v1/design.html`; this file is the part a builder needs
without opening them: the principles, the pages, the look's tokens and the anatomy of each repeated element. Where this
file and `SPEC.md` differ, the spec wins.

## What the app is

The user side of litwatch: where the value the system produces is handed over. Litwatch reads five to six hundred
papers a night so that one person does not have to, and the app is how they get the result: what is new in their
field, which papers matter for their question, and enough about each to decide whether to read it. The pipeline that
produces this is the kitchen; it is reachable through Settings and not on the table.

## Principles

| Principle | What it rules out |
| --- | --- |
| **One voice.** Litwatch speaks as itself: "this paper does X; it matters for your question because Y." | Grade names, judge and check, split, model names, reason strings on a reading page, with two exceptions: the status line's split count and the folded "how this was chosen" on a paper page. A split verdict is shown at the standing grade with no mark. |
| **Written where it is read, tabular where it is browsed.** The Digest is prose with headlines; a topic is a table. | Verdict rows on the Digest; prose where a sortable list is wanted. |
| **Offered, never asked.** Feedback is always available and never requested. | Label queues, "mark as read", anything that accumulates as a backlog. |
| **All quiet, at a glance.** One line says whether the run finished and whether it left anything for Quality. | Run logs, costs and recall on the front. |
| **Kitchen behind a door.** Topic configuration, quality, runs, integrations, misses are Settings pages. | Quality and System as top-level navigation. |
| **Self-contained.** With no integration connected, every page works. | Any page that assumes the archive, Nextcloud, n8n or a deep-card provider. A stored deep card is still shown; only the request button needs a provider. |
| **Phone first, desktop whole.** One column on the phone; on a desktop the width is used for a reading pane and real tables. | A desktop that is a stretched phone, or a phone that is a cut-down desktop. |

## Navigation

Digest, Topics, Papers, and a gear. A footer with three buttons on the phone; three words in the header on a desktop,
with the status chip beside them. The gear holds Settings: Topics, Quality, Misses, Runs and cost, Integrations,
Configuration. The levels are Digest → issue → paper and Topics → topic → paper, each one tap apart. Nothing from behind
the gear appears in front of it.

## Pages

**Digest.** A feed of issues, newest open, earlier ones folded to date and lede. An issue is a record: an entry whose
paper was later set aside is kept, struck through, with "set aside". A paper relevant to two Watch topics has an entry
in each section, each with that topic's take and "also in <other topic>" in the meta line. The masthead carries the open issue's
date and the status line. The most recent weekly issue is pinned as a compact row above the feed, labelled with its
week, and absent until one exists. Before any issue exists the Digest says when the first run is. A
daily issue is: lede; one section per Watch topic, in the owner's order, with lead entries (must read, a two or three
sentence take) and brief entries (relevant, one sentence); a fold line counting the adjacent papers and linking to the
topic pages; one section per daily Radar topic with its review; a closing line. A topic with nothing new is one line,
never an empty section. On a day with no issue the last one is shown and the masthead says "nothing new since". No
"mark read". On a desktop the feed keeps its reading width and the right half is a reading pane.

**Paper.** Title; topic chips in the topic's colour; authors, arXiv id, dates, version; the action bar (Save, arXiv,
PDF, Copy BibTeX); the take, one per Watch topic the paper is relevant to, each under its topic's chip; the abstract;
the deep card when one is stored, else a "Request a deep card" button while a deep-card integration is loaded, else
nothing; published version; history; "How this was chosen" as one quiet link that unfolds the
triage answer, both opinions by model name, the reconciled grade and an optional label row; thumbs at the end.

**Topics.** On the phone one screen of cards grouped Watch, Radar, Pulse in the Digest's order: colour bar, name, kind
and cadence, one line of what is new since last opened, counts. Paused topics sink and fade. On a desktop a rail with the
same list and the chosen topic open beside it; the tab lands on the topic last open. A topic page is the papers table
with the topic fixed, the subject as its intro line with two counts, filters All / Saved / Also adjacent / year, search
within, Export BibTeX, and a gear to the editor in Settings. A Radar or Pulse topic shows its reviews as dated prose
above the table, which holds the whole field with the picks first and marked.

**Papers.** The whole collection as the same table with a search box and filters for topic, saved, year and source.
Saved is the filter that makes it the library. Export BibTeX acts on the filter or selection and covers collection rows,
including archive-only rows as arXiv entries built from the hit's metadata. Without a query the table sorts by added date; a search sorts by published
date, newest first, or by the provider's score under "best match" when every hit in the band has one. The archive's hits
merge in when it is connected; an archive-only row is marked outside the collection with Fetch as its action and no
added date; archive-only hits sit in their own band under the collection rows, and the year filter is the only one
that reaches them. "Should have been found for a topic…" on a row, and "Look up an arXiv id or link" under the table,
are the miss flow; the Android share target lands on the lookup with the link filled in. The amber link "Should have been found for a topic…"
on a row starts a miss report; "Look up an arXiv id or link" under the table fetches a paper and offers the same.

**Settings.** An index on the left, plain pages on the right, in the interface sans. Topics (list and editor: subject,
rubric, keywords with match counts; preview against labelled papers and misses; scoped re-runs after saving; pause,
order and colour). Quality (at the top, last night: a failure summary linking to the run when it failed, the splits with
both opinions and the label row, ungraded candidates with "grade it now", papers awaiting triage with Dismiss, Triage by hand and Fix the text, a review that failed; then **Calibrate**;
below, recall and precision per Watch topic over evaluation runs, each number a link to its papers; correction rate
from reactions; the "matters, no topic" list with its notes). Misses (the stage trace per report). Runs and cost. Integrations (one card per
module: enabled, description, cursor lag, failures with last error, retry and skip, search provider statistics; the
outbox table under them). Configuration (the loaded `litwatch.toml`, secrets masked, read-only).

## Look: Masthead

Warm paper, a newspaper masthead, a colour per topic, serif where text is read and the system sans where it is
interface. Dark is the same on a dark ground, following the system preference.

### Tokens

| Token | Light | Dark | Used for |
| --- | --- | --- | --- |
| `--background` | `#faf7f1` | `#15171a` | page |
| `--foreground` | `#1a1a18` | `#e9e6df` | text, the masthead rule, primary buttons |
| `--muted-foreground` | `#6f6c64` | `#9a9b96` | meta lines, labels, the printer's line |
| `--border` | `#dcd6c9` | `#2b2f34` | rules between entries and rows |
| `--muted` | `#f1ece1` | `#1d2024` | the pinned weekly row, the open entry on a desktop, chips' background |
| `--primary` | `#2f5d50` | `#7fc4ae` | the one accent: Read, Fetch, retry |
| `--success` | `#2e7d4f` | `#5fb57f` | the status dot when quiet, the Saved mark, the relevant dot |
| `--warning` | `#b7791f` | `#e0a94a` | the status dot and count on a split, the miss link |
| `--destructive` | `#b2412f` | `#e06a55` | the status dot on a failed run |
| `--font-serif` | Charter, "Iowan Old Style", "Source Serif 4", Georgia, serif | | masthead, dates, titles, ledes, takes, reviews |
| `--font-sans` | ui-sans-serif, system-ui, "Segoe UI", Roboto, sans-serif | | everything that is interface |
| `--font-mono` | ui-monospace, SFMono-Regular, Menlo, monospace | | keywords, error text, configuration |

Grade dots on tables, never with a word beside them: must read `--foreground`, relevant `--success`, adjacent
`#c9c7c0`, not a candidate an empty ring in `--border`.

### Topic colours

Each topic gets one colour at creation, the first of this list not in use, stored on the topic and changeable in its
editor; its position in the list is the owner's order. It appears as the rule above the topic's
section on the Digest, the bar on its card, the bar on its rail entry when chosen, its name on its page, and its chip
on a paper page.

| | Colour | First assigned to |
| --- | --- | --- |
| 1 | `#2f5d50` green | CoT monitoring |
| 2 | `#8a3b2e` oxblood | CoT faithfulness |
| 3 | `#3b4f8a` indigo | Interpretability |
| 4 | `#a2661e` ochre | Agents |
| 5 | `#5a6b3a` olive | Alignment and safety |
| 6 | `#7a4f8a` plum | Efficiency |
| 7 | `#2f6b7a` teal | Reasoning |
| 8 | `#8a6a3b` tan | |

More than eight topics cycle; a paused topic shows grey.

### Type scale

| Element | Face | Size (phone / desktop) | Weight |
| --- | --- | --- | --- |
| Masthead brand | serif | 28 | 700, letter-spaced |
| Issue date, topic name | serif | 28 / 30 | 600 |
| Lede | serif | 16 / 17 | 400 |
| Section heading | serif | 20 | 600, with its topic rule |
| Lead entry title | serif | 17 / 19 | 600 |
| Brief entry title, table title | serif | 15.5 / 16.5 | 600 |
| Take | serif | 15.5 | 400 |
| Review prose | serif | 16 | 400 |
| Meta line, action row | sans | 13 | 400 |
| Status line | sans | 11, uppercase, letter-spaced | 400 |
| Table cell | sans | 13.5 | 400 |
| Settings | sans | 13.5 | 400 |

### Anatomy

**Status line** (the printer's line under the masthead; the chip in the header elsewhere): a dot and a sentence about
the latest nightly run. Green: "530 read · all quiet", the count being papers triaged in that run. Amber, with a
count, when the run finished but left something for Quality: "530 read · 2 disagreements" (split verdicts among the
papers shown; which splits count is spec decision 14), "· 1 ungraded", "· 1 awaiting triage", "· interpretability review failed". Red: "last
night's run failed · showing Tuesday", also when no run finished in the last 36 hours. Grey while running: "running
since 06:02"; on a fresh install "no run yet". Red wins over amber. It always opens Quality, and opening an old issue
does not change it.

**Entry** (one paper on the Digest): title; meta line (authors with "and N more", date, "revised, first seen …" when
so); take; action row: Read, arXiv, PDF, Save or Saved (tapping Saved unsaves), BibTeX, then the thumbs at the right
edge. On a desktop the
title is the link and Read is dropped. Lead and brief entries differ in title size and take length only.

**Thumbs**: two 16 px glyphs in 40 px hit areas, quiet until used, on every paper wherever it is shown, for the topic
of that surface or for none on a plain search result; only a Watch-topic reaction counts as the owner's word. A
thumb down opens the reasons as chips: Not for me, Unrelated to the topic, Already knew it, Overrated; the first tap on
a reason closes it. The acknowledgement says what happened: "Remembered for <topic>" (or "Remembered") for the reasons
that carry no word, "Marked not relevant for <topic>" for Unrelated, followed by "; it leaves the library" only when
it does (another topic or a save by hand can keep it). A thumb up: the first tap sets it, a second offers Great find
as a chip beside it, a third clears it. A thumb down: the first tap sets it and opens the reasons, a second clears it.
So every state is reachable by tap and by keyboard, and clearing a thumb takes back the reaction and the word it
carried, and nothing else.

**Row** (one paper in a table): grade dot (ink must read, green relevant, pale adjacent, an empty ring for a paper
that is no topic's candidate; the word "grading" in the muted colour for a pending candidate; Radar and Pulse picks
carry no dot); title with authors under it in the meta style, or "near the topic" for an adjacent row; published; added
(empty for an archive-only row); take, that topic's on a topic page and the first Watch topic's on Papers, empty
where there is none, with "also in <other topic>" where the paper has more than one; Saved; link icons (arXiv, PDF, code when present).
Sort by any column; default Added descending. A topic table shows relevant and above, adjacent rows until "Also
adjacent" is on in the muted colour, and nothing below; the Papers table shows every paper.

**Topic card**: a 4 px colour bar; name with kind and cadence in the meta style; the new line ("3 new since Tuesday,
1 saved": entries in issues since the topic was last opened, which the server remembers so every device agrees; "This
week's review, Monday"; "Nothing new" in the muted colour); counts right-aligned (papers and saved for Watch; picks and
saved for Radar and Pulse). **Rail entry**: a 3 px bar that shows only when chosen, name, and either a
"N new" count in bold or the cadence in the muted colour.

**Issue in the feed**: date in serif, "weekly" in the meta style when so, "Open" at the right, the lede under it.

**Calibrate** (Settings → Quality): one paper per screen, title, abstract and its takes, then the questions in the
order of the spec's table (section 8, M3): fit as a row of chips per Watch topic, the required one first; value as
three chips; accurate or off beside each take; a note field. Next and Back, a count ("7 of 20"), and Done, which
shows what the session changed. Answers save as they are given, so leaving mid-session loses nothing.

**Integration card**: name, enabled state and target in the meta style; one-line description from the module; status
line with a dot (green up to date, amber with failures, grey not loaded); the last error in mono; Retry now, Skip this
event, All events.

### Phone

One column, 16 px gutters, the three-button footer, the gear in the header. The paper page is a full page with a back
link. Table rows stack as title, authors, take, one small line of dates, the saved mark; sort and filters sit in a sheet
behind one button. Every control has a hit area of at least 40 px. The app is an installable PWA; the Android share
target opens Papers with the shared link looked up, and the token prompt on first use keeps that route (spec,
decision 4).
