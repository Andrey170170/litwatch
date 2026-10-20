# litwatch

A literature watch: it reads every new arXiv paper in its categories, sorts and judges each one, and hands over what
matters at the level of detail the owner needs at that moment.

## Papers

**Collection**:
The papers litwatch has processed, each kept with the text exactly as it was judged; nothing unprocessed is kept.

**Archive**:
The store of arXiv papers (metadata, LaTeX text, PDFs) that downloads, keeps and searches papers for litwatch; a
separate service that ships with litwatch and starts empty.
_Avoid_: archived (for papers litwatch dropped; say "not processed")

## Classes and topics

**Taxonomy**:
The tree of classes every paper is sorted into, from broad areas at the top to narrow questions at the leaves.

**Class**:
One node of the taxonomy; a paper in a class is also in all of its ancestors.
_Avoid_: Category, tag, node (in prose)

**Primary class**, **secondary class**:
The one class a paper belongs to most, and any others it also belongs to.

**Topic**:
A class the owner follows, which gets digests covering every paper in its subtree, at a depth of attention the
owner sets: Focus, Area or Field.
_Avoid_: Selection (0.x's word)

**Focus topic**:
The owner's current research question: every paper central to it is shown, nothing may be missed.
_Avoid_: Watch topic (0.x/v1 draft word)

**Area topic**:
The owner's wider research area: its main trends and the day's one or two papers most worth attention.
_Avoid_: Radar topic

**Field topic**:
A broad field the owner keeps up with: a high-level overview of the week and a handful of picks.
_Avoid_: Pulse topic

**Digest**:
One topic's written overview of one day or one week, with the papers it points to; a day with nothing new still
gets one saying so.
_Avoid_: Issue, review, recap, briefing

**Pick**:
A paper a digest puts forward by name, as opposed to papers its prose only mentions.

**Due**:
Eligible for a topic's next digest and not yet in a published one; a late paper is due in the next digest.

## Judging a paper

**Fit**:
How close a paper is to one topic: central, related, adjacent, none. The topic's subject defines it; a Focus topic's
rubric spells out central, related and adjacent.

**Weight**:
How much a paper matters in its own area (its primary class), whatever topic it is graded for: landmark, solid,
incremental. Judged from the abstract for every paper, and judged again from the full text by a Brief or a Weight
re-check.

**Full-text pass**:
Any work that reads a paper's full text rather than its abstract (a Brief, a Weight re-check, a Deep card); it runs
as soon as the text is available, and until then the paper is awaiting text.

**Grade**:
A paper's standing within one class, from its Fit there and its Weight: must read, relevant, adjacent, none.
_Avoid_: Tier (0.x's word), score

**Rarity**:
A paper's overall standing across the classes the owner follows, shown as a colour.

**Assurance**:
How strictly a grade is checked, taken from the nearest followed class at or above the graded class.

**Badge**:
A topic's icon on a paper, saying which followed topics it belongs to.

**Facet**:
A fact about a paper that is not a judgement: its kind of contribution, and whether it released code or weights.

**Effective Fit**, **effective grade**:
The Fit and grade every page, export and integration uses: the owner's word on Fit where there is one, otherwise the
verdict's, with the grade derived from that Fit and the current Weight.

## The owner's corrections

**Owner's word**:
The owner's own statement of a paper's Fit for a topic, from a label, a reaction that carries one, or a reported
miss; it outranks any verdict.

**Reaction**:
A thumb up or down on a paper for the topic it was shown under, with an optional reason naming what went wrong.
_Avoid_: Vote, rating

**Label**:
The owner's deliberate answer on a paper's Fit for a topic, or on its classes, given in Calibrate or on the paper page.

**Miss**:
A paper the owner found elsewhere that litwatch should have surfaced for a topic.

**Calibrate**:
A session in which litwatch asks the owner about a drawn sample of papers, the only place it asks.

**Library**:
The papers marked saved: Focus papers at relevant or above, and whatever the owner saves by hand, less what the
owner unsaved.
_Avoid_: Saved list, reading list

## Detail levels

**Detail level**:
One rung of the ladder of text about a paper or a digest, each answering "is it worth going one level deeper?" with
more evidence than the last. A paper's: Mark, Gist, Brief, Deep card, then the paper itself. A digest's: Mark, Gist,
then the digest itself. A rung's name is its role; its length depends on how many items are scanned at once.
_Avoid_: LoD, summary level

**Mark** (L0):
What an item shows at a glance. A paper's: its title, its rarity, and its badges where the list mixes topics. A
digest's: its topic badge, how many papers it covers and how good the best of them is.

**Gist** (L1):
What is skimmed before opening an item. A paper's is about one sentence (20–30 words) on what it shows, independent
of any class, shown with its publication date; it is written from the abstract, and again from the full text when the
paper gets a written Brief. A digest's is a short paragraph.
_Avoid_: Take, one-liner, TL;DR

**Brief** (L2):
What the owner reads on opening a paper to decide whether to read it: the abstract, or a short account written from
the full text by a Brief pass or a Weight re-check (for a Focus paper at central or related, an Area pick
candidate).
_Avoid_: Semi-deep, summary

**Deep card** (L3):
An account of a paper thorough enough to stand in for reading most of it, placed among the papers around it; made
for a selected few.
_Avoid_: Deep, reading card

## The Notion export

**Notion export**:
The Notion database the `notion` integration keeps for a reader who does not use litwatch. It uses its own plain
words, and only there: **Must read** and **Worth a look** for the grades must read and relevant, **No longer
listed** for a paper that fell below them, **TL;DR** for the Gist, **Deep read** for the Deep card; a page shows the
abstract, not the Brief.
_Avoid_: Worth a look, No longer listed and TL;DR anywhere else. Must read is the grade's own name everywhere; Deep
read keeps its web-app meaning elsewhere: the action that asks for a Deep card.
