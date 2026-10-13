# Digests

A digest is one topic's written overview of one day or one week. It is what the Digest tab is made of. What a digest
holds depends on the topic's depth, because the depth is how closely the owner reads that topic.

## What each depth gets

| Depth | Daily digest | Weekly digest |
| --- | --- | --- |
| Focus | Every paper at Fit central that day, each with its Gist; papers at Fit related as one line each; a short text on what the day brought | What the week was about: the thread through the week's papers, with the chance to bring back an earlier paper the owner may have forgotten; then the week's central papers |
| Area | A short text on what happened in the area that day, which may mention any of the day's papers by name; 1–2 picks | The week's main trends; 5 picks |
| Field | none | A high-level overview of the week; 3 picks |

**Focus.** The list is driven by Fit, not by grade: a central paper is listed even when its grade is relevant because
its Weight is incremental, so nothing central is ever hidden. Rarity only colours it. Adjacent papers are never in a
digest; they are on the topic page.

**Area picks.** The candidates are the day's papers in the topic's subtree whose grade is must read after the
full-text Weight re-check ([pipeline.md](pipeline.md), section 6). The writer names the one or two that matter most
and is asked to keep it to that, with no hard cap and no quota: a day with no candidate has no pick. The text may
cover more of the day's papers in prose. Weekly picks (5 for Area, 3 for Field) are chosen from the whole week's
members by the writer.

**Children of a followed topic.** A parent topic's digest covers its subtree, including a child topic's papers, at the
child's grade. A CoT faithfulness paper that is must read appears among Interpretability's pick candidates at its
CoT faithfulness grade; Interpretability's text discusses it only if it matters to the area as a whole.

## Quiet days

- A day on which arXiv announced nothing (every Saturday and Sunday, and holidays) has no digests, only one line for
  the whole day: "No papers released."
- A day on which arXiv announced papers but a topic got none (or none central, for Focus) gives that topic a digest
  that says "Nothing new". It is a real digest, so the owner can see that the run happened and the topic was looked at.

## Writing

One call per digest to the `writer` role, Opus 5.5 by default (first v1, decision 21). The writer reads, per paper,
its Gist, grade, Fit reason and Brief where one exists, and for a weekly digest the week's daily digests. The voice is
objective and does not address the reader: no "for you", "for X builders", "required reading". Grades, model names,
splits and other pipeline terms do not appear in digest text.

Each digest has a **Gist** of its own: a short paragraph, longer than a paper's one sentence because only a few
digests are read a day. It is written in the same call.

## Timing

The nightly run ends with grading. Digests are written after a **bounded wait** for the full-text passes the digest
depends on: Focus Briefs and the Area Weight re-checks. The wait ends when they are all done, or after
`[digests] wait_minutes` (default 120), whichever comes first. A pass that finishes later updates its paper, not the
digest. Weekly digests are written on Monday after that night's daily digests, for the Monday-to-Sunday week before,
in the configured timezone.

## A digest is a record

A digest stays as it was written. When the owner later sets one of its papers below relevant, the paper stays in it,
shown struck through as set aside. A paper is listed as new in a topic's daily digest once: a re-run of the same day
amends the digest and never repeats a paper, and a later night does not announce it again (first v1, decisions 15
and 19). A digest whose writer call failed says so in one line and is filled in when the job runs again.

## What the Digest tab shows

The detail-level ladder for digests, and the order rules the mechanics must support. The pages themselves are the UI
pass's.

1. **History**: days, newest first. Each day lists its topics' digest **Marks**: badge, how many papers, and the
   colour of the best of them ("4 new · 1 must read", "Nothing new").
2. **A day**: each topic's digest **Gist**, followed by the Marks of its picks (up to 3) and a small count of the rest.
3. **A digest**: its full text, its picks and listed papers at their Mark and Gist.
4. **A paper**: the paper page, from its Brief down.

The tab opens on today. Topics keep **one fixed order** everywhere (Focus first, then Area, then Field, each in the
owner's order), and a topic with nothing new keeps its slot, so a topic is always in the same place. Weekly digests sit
in the group of the Monday they were written, marked as a week, and the current week's are also pinned above History
until the next Monday.
