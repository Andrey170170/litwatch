# Updated papers and Needs you's folds: follow-ups

Found while building [`docs/plans/updated-papers.md`](../plans/updated-papers.md) (#106, #112, #115) and left as they
are, with the owner's agreement on 2026-10-20. None of them is a defect in what the plan asked for.

- **The Acknowledged section's fold on Needs you** keeps its own look from before #106. On the phone its head is
  34 px tall, where the app's tap targets are 40 (DESIGN.md, "Phone"). Folding it unmounts its groups, so their own
  fold choices inside it reset each time it closes.
- **History's rows on the phone** cut long topic names harder since the count line gained "· N updated", for example
  "Chain-of-th…" beside "3 new · 1 must read · 2 updated". The topic badges still tell the rows apart.
- **A Field weekly's updated list** sits after its "Also this week" list, which runs to about 2,000 rows in
  language-models, so reaching it takes a long scroll. That list was as long before #115.
- **Titles with TeX** show raw: one production title begins `$\mathrm{R}{\scriptstyle…`. This predates the plan and
  affects every list that shows a title.

A hard reload resets the folds' session memory, as it does the topic page's shelves; the owner is fine with that.
