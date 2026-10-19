# Digests

A digest is one topic's written overview of one day or one week. It is what the Digest tab is made of. What a digest
holds depends on the topic's depth, because the depth is how closely the owner reads that topic.

## What each depth gets

| Depth | Daily digest | Weekly digest |
| --- | --- | --- |
| Focus | Every due paper at Fit central, each with its Gist; due papers at Fit related as one line each; a short text on what the day brought | What the week was about: the thread through the week's papers, with the chance to bring back an earlier paper the owner may have forgotten; then the week's central papers |
| Area | A short text on what happened in the area that day, which may mention any due paper by name; 1–2 picks | The week's main trends; 5 picks |
| Field | none | A high-level overview of the week; 3 picks |

Every Area topic gets both, every Field topic the weekly only; a depth has no other cadence setting.

**Focus.** The list is driven by the effective Fit, not by grade: a central paper is listed even when its grade is
relevant because its Weight is incremental, so nothing central is ever hidden. Rarity only colours it. Adjacent papers
are never in a digest; they are on the topic page.

**Area picks.** The candidates are the due papers in the topic's subtree whose grade is must read after the full-text
Weight re-check ([pipeline.md](pipeline.md), section 6). The writer names the one or two that matter most and is asked
to keep it to that, with no hard cap and no quota: a day with no candidate has no pick. The text may cover more of the
due papers in prose. Weekly picks (5 for Area, 3 for Field) are chosen by the writer from the papers that became due
during the week.

**Children of a followed topic.** A parent topic's digest covers its subtree, including a child topic's papers, at the
grade a parent shows ([pipeline.md](pipeline.md), section 4). A CoT faithfulness paper that is must read appears among
Interpretability's pick candidates at its CoT faithfulness grade; Interpretability's text discusses it only if it
matters to the area as a whole.

## Which papers a digest covers

A paper is **due** for a topic once the pipeline has made it eligible there, and stays due until a published daily
digest of that topic includes it, or until it stops being eligible (the owner's word sets it below related, a class
label takes it out of the subtree). Eligible means:

| Depth | Eligible |
| --- | --- |
| Focus | graded for the topic with effective Fit central or related |
| Area | a member of the subtree that grading has finished for; a paper graded must read from the abstract only once its Weight re-check is done or terminal (below) |
| Field | a member of the subtree that grading has finished for |

A **daily digest** covers the topic's due papers at the time it is written. Normally those are the day's papers; a
paper that became eligible late (its grading failed on Friday and succeeded on Monday, its re-check finished after the
wait) is in the next daily digest, shown with its announcement date. A **weekly digest** covers the papers that became
due for the topic during its week, and any paper a published daily digest covered that no weekly digest has yet (a
daily digest written again after its week's weekly, the days before a recovery window's first Monday), so none is
left out of every weekly; for a Field topic, which has no daily digest, writing the weekly is what clears
them. The owner's word never makes a paper due: the owner already knows it. A paper announced more than three weeks
before a digest's day is never due for it: late papers arrive within days, and one the collection gains much later (a
re-run after a topic edit, a reported miss, an old paper read on demand) was never news. A paper's day is the arXiv
listing that first showed it, from its first version's submission time (before 14:00 US Eastern on a weekday, the
next weekday's listing). arXiv's holidays are not known: the papers a holiday delays keep the holiday's date and
are in the next day's digests.

## Quiet days

- A day on which arXiv announced nothing (every Saturday and Sunday, and holidays) has no digests, only one line for
  the whole day: "No papers released."
- A day on which arXiv announced papers but a Focus or Area topic has nothing due (for Focus: no central and no related paper) gives
  that topic a digest that says "Nothing new". It is a real digest, so the owner can see that the run happened and the
  topic was looked at. A Focus day with related papers and no central one is an ordinary digest whose text says so.

## Writing

One call per digest to the `writer` role, Opus 5.5 by default (first v1, decision 21). The writer reads, per paper,
its Gist, grade, Fit reason and Brief where one exists, and for a weekly digest the week's daily digests. The voice is
objective and does not address the reader: no "for you", "for X builders", "required reading". Grades, model names,
splits and other pipeline terms do not appear in digest text.

Topics' digests are written concurrently, each topic on its own database connection and in its own order (its failed digests tried again, the day's
digest, Monday's weekly), so the nightly run's writer calls go out in a few batches ([models.md](models.md), batches)
rather than one after another. When the wait runs to the nightly's deadline, the digests are written after it as
normal calls at full price (about $0.50 a night).

Each digest has a **Gist** of its own: a short paragraph, longer than a paper's one sentence because only a few
digests are read a day. It is written in the same call.

## Timing

The nightly run ends with grading. Digests are written after a **bounded wait**, which starts when that run's grading
ends, for the full-text passes the night queued that digests depend on: Focus Briefs and the Area Weight re-checks.
The wait ends when each of them has finished, has failed in this run (it is retried on the next run, so waiting longer
gains nothing), or is terminal (three failed runs; refused by every model of `brief`'s list; text awaited for more than seven days), or at the nightly run's deadline (`[schedule] deadline`, default 07:30; [models.md](models.md), batches), whichever
comes first. Passes already under way at the deadline finish first (their cancelled batch's requests go as normal
calls), so digests carry them and are written by about 08:00; a pass that finishes later updates its paper, not
the digests. A run without a deadline waits at most `[digests] wait_minutes` (default 120).

- A Focus paper without its Brief at that point is listed anyway; the writer reads its abstract instead.
- An Area paper whose re-check has not finished, or failed in this run, is not due yet: it becomes due when the re-check finishes, and is a
  pick candidate in the next daily digest if it is still must read. A re-check that becomes terminal makes the paper
  due on its abstract Weight, so a failure never hides a paper.
- A pass that finishes later updates its paper, not a digest already published.

Weekly digests are written on Monday after that night's daily digests, for the Monday-to-Sunday week before, in the
configured timezone.

## A digest is a record

A digest is **published** when it is written or says "Nothing new". A published digest is final: a re-run of the
same day does not rewrite it, and papers that become due afterwards go into the next one. Each of its papers is frozen
as it was shown: paper revision, title, Gist, effective Fit, grade, rarity, and its role (pick, listed or mentioned). The one
live element is the set-aside mark: when the owner later sets a paper below relevant, it stays in the digest, shown
struck through. A paper is announced as new in a topic's daily digests once (first v1, decisions 15 and 19).

A digest whose writer call fails, or returns text that does not parse, is **failed**, not published: the day shows
one line saying that topic's digest could not be written, it is listed on Quality, and the next run writes it again
over the papers due then. Its papers stay due meanwhile, so nothing is lost or announced twice.

## What the Digest tab shows

The detail-level ladder for digests, and the order rules the mechanics must support. The pages themselves are
[`DESIGN.md`](DESIGN.md)'s.

1. **History**: days, newest first, as a list or a month calendar. Each day shows its topics' digest **Marks**: badge,
   how many papers, and the colour of the best of them ("4 new · 1 must read", "Nothing new"); the calendar shows the
   colours alone where it is too narrow for the rest.
2. **A day**: each topic's digest **Gist**, followed by the Marks of its picks (up to 3) and a small count of the rest.
   **A week** is the same for the week's weekly digests.
3. **A digest**: its full text, its picks and listed papers at their Mark and Gist.
4. **A paper**: the paper page, from its Brief down.

The tab opens on today. Topics keep **one fixed order** everywhere (Focus first, then Area, then Field, each in the
owner's order), and a topic with nothing new keeps its slot, so a topic is always in the same place. Weekly digests sit
in the group of the Monday they were written, marked as a week, and the current week's are also pinned above History
until the next Monday.
