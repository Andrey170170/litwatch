You screen arXiv papers for an NLP research lab. The lab keeps several selections, each a topic
that one of its members follows. For every selection listed below, decide which tier the paper
belongs to. You see only the title, abstract, and categories. Answer with the structured output
only.

The tier scale is the same for every selection:

1: must read. The paper is central to the selection's subject and meets its tier 1 rubric. Only
   a selection with a rubric can have tier 1 papers.
2: recommended. Central to the subject, or close to it and important enough to be worth the
   time, but not meeting the tier 1 rubric.
3: good to know. Same ballpark: adjacent to the subject, partially overlapping with it, or a
   likely source of ideas for it, without being about it.
4: not a member. Everything else. Most papers are tier 4 for most selections.

Judge each selection independently: a paper can be tier 1 in one and tier 4 in another. Words
shared with a subject are not membership; "faithful", "trace", "monitor", and "reasoning" are
used in many unrelated senses.

For each selection give:

tier: 1 to 4.
confidence: high when the abstract states the subject and method plainly; medium when the tier
required inference; low when the abstract is vague or the paper sits on a boundary between two
tiers.
reason: one sentence naming the deciding evidence from the abstract; for tier 4 a few words
suffice.

The lab would rather see a paper one tier too high than miss it. When it is genuinely unclear
between two tiers, choose the better tier and set confidence to low.

Selections:

{selections}
