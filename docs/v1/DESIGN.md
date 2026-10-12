# litwatch v1 web app design

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
| **One voice.** Litwatch speaks as itself: "this paper does X; it matters for your question because Y." | Grade names, judge and check, split, model names, reason strings on a reading page. A split verdict is shown at the standing grade with no mark. |
| **Written where it is read, tabular where it is browsed.** The Digest is prose with headlines; a topic is a table. | Verdict rows on the Digest; prose where a sortable list is wanted. |
| **Offered, never asked.** Feedback is always available and never requested. | Label queues, "mark as read", anything that accumulates as a backlog. |
| **All quiet, at a glance.** One line says whether the run finished and whether the graders seriously disagreed. | Run logs, costs and recall on the front. |
| **Kitchen behind a door.** Topic configuration, quality, runs, integrations, misses are Settings pages. | Quality and System as top-level navigation. |
| **Self-contained.** With no integration connected, every page works. | Any page that assumes the archive, Nextcloud, n8n or a deep-card provider. |
| **Phone first, desktop whole.** One column on the phone; on a desktop the width is used for a reading pane and real tables. | A desktop that is a stretched phone, or a phone that is a cut-down desktop. |

## Navigation

Digest, Topics, Papers, and a gear. A footer with three buttons on the phone; three words in the header on a desktop,
with the status chip beside them. The gear holds Settings: Topics, Quality, Misses, Runs and cost, Integrations,
Configuration. The levels are Digest → issue → paper and Topics → topic → paper, each one tap apart. Nothing from behind
the gear appears in front of it.

## Pages

**Digest.** A feed of issues, newest open, earlier ones folded to date and lede. The masthead carries the open issue's
date and the status line. This week's weekly issue is pinned as a compact row above the feed from Monday to Sunday. A
daily issue is: lede; one section per Watch topic, in the owner's order, with lead entries (must read, a two or three
sentence take) and brief entries (relevant, one sentence); a fold line counting the adjacent papers and linking to the
topic pages; one section per daily Radar topic with its review; a closing line. A topic with nothing new is one line,
never an empty section. On a day with no issue the last one is shown and the masthead says "nothing new since". No
"mark read". On a desktop the feed keeps its reading width and the right half is a reading pane.

**Paper.** Title; topic chips in the topic's colour; authors, arXiv id, dates, version; the action bar (Save, arXiv,
PDF, Copy BibTeX); the take; the abstract; the deep card when one exists, a "Request a deep card" button when a provider
is configured, nothing otherwise; published version; history; "How this was chosen" as one quiet link that unfolds the
triage answer, both opinions by model name, the reconciled grade and an optional label row; thumbs at the end.

**Topics.** On the phone one screen of cards grouped Watch, Radar, Pulse in the Digest's order: colour bar, name, kind
and cadence, one line of what is new since last opened, counts. Paused topics sink and fade. On a desktop a rail with the
same list and the chosen topic open beside it; the tab lands on the topic last open. A topic page is the papers table
with the topic fixed, the subject as its intro line with two counts, filters All / Saved / Also adjacent / year, search
within, Export BibTeX, and a gear to the editor in Settings. A Radar or Pulse topic shows its reviews as dated prose
above the table, which holds the picks.

**Papers.** The whole collection as the same table with a search box and filters for topic, saved, year and source.
Saved is the filter that makes it the library. Export BibTeX acts on the filter or selection. The archive's hits merge in
when it is connected; an archive-only row is marked outside the collection with Fetch as its action; archive-only hits
sit in their own band under rows that litwatch's filters apply to. The amber link "Should have been found for a topic…"
on a row starts a miss report; "Look up an arXiv id or link" under the table fetches a paper and offers the same.

**Settings.** An index on the left, plain pages on the right, in the interface sans. Topics (list and editor: subject,
rubric, keywords with match counts; preview against labelled papers and misses; scoped re-runs after saving; pause and
order). Quality (last night's splits with both opinions and the label row; ungraded candidates with "grade it now";
recall and precision per Watch topic over evaluation runs, each number a link to its papers; correction rate from
reactions; "draw a calibration sample"). Misses (the stage trace per report). Runs and cost. Integrations (one card per
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

Each topic gets one colour at creation, from this list in order, and keeps it. It appears as the rule above the topic's
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

**Status line** (the printer's line under the masthead; the chip in the header elsewhere): a dot and a sentence.
Green: "530 read · all quiet". Amber: "530 read · 2 disagreements", when the two graders were more than one grade
apart on a paper shown. Red: "last night's run failed · showing Tuesday". It always opens Quality.

**Entry** (one paper on the Digest): title; meta line (authors with "and N more", date, "revised, first seen …" when
so); take; action row: Read, arXiv, PDF, Save or Saved, BibTeX, then the thumbs at the right edge. On a desktop the
title is the link and Read is dropped. Lead and brief entries differ in title size and take length only.

**Thumbs**: two 28 px targets (32 on the phone), quiet until used. A thumb down opens the reasons as chips: Not for
me, Unrelated to the topic, Already knew it, Overrated; the first tap on a reason closes it, with "Remembered for
<topic>. Changes nothing today." A thumb up stands alone; a long press offers Great find.

**Row** (one paper in a table): grade dot; title with authors under it in the meta style, or "near the topic" for an
adjacent row; published; added; take; Saved; link icons (arXiv, PDF, code when present). Sort by any column; default
Added descending. Adjacent rows are hidden until "Also adjacent" is on, and shown in the muted colour.

**Topic card**: a 4 px colour bar; name with kind and cadence in the meta style; the new line ("3 new since Tuesday,
1 saved"; "This week's review, Monday"; "Nothing new" in the muted colour); counts right-aligned (papers and saved for
Watch; picks and saved for Radar and Pulse). **Rail entry**: a 3 px bar that shows only when chosen, name, and either a
"N new" count in bold or the cadence in the muted colour.

**Issue in the feed**: date in serif, "weekly" in the meta style when so, "Open" at the right, the lede under it.

**Integration card**: name, enabled state and target in the meta style; one-line description from the module; status
line with a dot (green up to date, amber with failures, grey not loaded); the last error in mono; Retry now, Skip this
event, All events.

### Phone

One column, 16 px gutters, the three-button footer, the gear in the header. The paper page is a full page with a back
link. Table rows stack as title, authors, take, one small line of dates, the saved mark; sort and filters sit in a sheet
behind one button. Thumbs and buttons are at least 40 px tall. The app is an installable PWA and the Android share
target lands on the miss flow with the link already looked up (spec, decision 4).
