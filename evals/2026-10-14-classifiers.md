# Classifiers compared (milestone N1)

2026-10-14, branch `feat/decide`. Three candidates for the `classify` role sorted the same 1,495 papers into the seed
tree (`src/litwatch/seeds/taxonomy.yaml`, 58 classes): one yes/no question per class and one choice of the primary
class, from title and abstract (pipeline.md, section 2). Dry runs: the answers are in
`evals/2026-10-14-classifiers/<candidate>.jsonl.gz`, one line per paper with every class's probability, so any
threshold can be scored again without new calls. The tables below are
`litwatch eval classify-report luna-decisions jev haiku --runs evals/2026-10-14-classifiers`.

## What was tested

Whether a decision model can be the classifier: whether it keeps the Focus topics' papers (the check: Focus
membership recall on the CoT positives, band and keyword check included, at least the first v1's candidate recall,
which lost 1 of the 90), how many papers it sends to grading, and whether its classes and primary class match the
labels. Each cohort is scored on its own (`evals/labels/README.md`):

- **owner**: the owner's own labels, 7 central papers on the two CoT topics. Too few to separate candidates.
- **provisional**: 0.14's tier 1–2 papers on the two CoT selections, 90 papers. Model output the owner has not
  confirmed: recall here means "keeps what 0.14 kept", not recall on the owner's word. `regression-legacy` is nearly
  the same papers.
- **silver**: the classes both labs (Opus 5.5, GPT-6.1-Sol) put a paper in, over the 198 papers both sorted; the
  primary class where they agreed (165); **adjudicated**: the owner's primary class for 20 papers they disagreed on.
- **0.14 draw**: 300 papers drawn from everything 0.14 screened (seed 20261014), for volume and for 0.14's selections.

| Candidate | How it is asked | Configuration |
| --- | --- | --- |
| luna-decisions | OpenRouter Decisions endpoint | `openai/gpt-6-luna-decisions` |
| jev | OpenRouter Decisions endpoint | `typesafe/jev-1.13` |
| haiku | Anthropic, chat with a strict schema | `claude-haiku-5-5`, medium effort |

Total spend: about $3.40 for the three runs and $0.20 for trials.

## Findings

- **The decision models say yes to too many classes at 0.5.** Jev averages 4.9 direct memberships a paper, Luna
  6.0, Haiku 2.0. On the silver memberships Jev's precision is 0.43 at recall 0.99; Haiku's 0.90 at 0.89. Asking
  "is X a main subject" instead of "does it belong to X" did not change this for Jev (precision 0.44 → 0.46 on the
  silver sample), so it is a threshold, not a wording, question.
- **Jev's probabilities spread; Luna's do not.** Luna answers nearly every class 0 or 1, so its threshold and band
  barely move anything: its Focus volume is the same from 0.3 to 0.6, and a miss at 0.0 can only come back by
  keyword. Jev's misses sit between 0.1 and 0.2, where a band can reach them.
- **Only Jev reaches the check.** With the band's floor at 0.1, Jev keeps all 90 provisional positives (and the 7 of
  the owner); Luna keeps 86, Haiku 83. Haiku misses CoT monitoring papers outright (41 of 51 with band and keywords).
- **Jev with a higher threshold outside the Focus subtrees keeps that recall and gains Haiku-like precision.**
  Classes at or under a Focus topic stay at 0.5 with the band from 0.1; every other class at 0.8: precision 0.72 at
  recall 0.96 on silver, 90/90 provisional, and Area grading falls from 37 to 19 gradings per 100 papers. At 0.9
  precision is 0.89 but recall 0.91.
- **The primary class is a wash.** 72–78 % exact on the agreed silver papers, 95–96 % in the right top-level class;
  on the 20 hard papers the owner settled, 15–30 % exact.
- **The misses are on the edge of the topic.** The seven provisional papers Luna or Jev lost at the 0.2 floor are
  mostly about a model's stated reasons or explanations rather than its chain of thought ("Does a model's stated
  reason for rejecting a candidate do any work?", "Necessary or Sufficient? Evaluating LLM Explanations With
  Behavioural Evidence"). 0.14 counted them; the Focus subject reads narrower. Jev gives them 0.1–0.2.
- **Cost and speed.** Per 1,000 papers: Jev $0.26, Haiku $0.69, Luna $1.31. Jev answers in 0.2 s, Haiku in 4.2 s.
  Haiku timed out at 5 s on connecting when run 8 at a time; it ran 3 at a time, without failures.

## Recommendation

The owner chose it on 2026-10-14.

**Jev, with classes at or under a Focus topic at 0.5, every other class at 0.8, and the uncertain band from 0.1.**
It is the only candidate that keeps every CoT positive, the cheapest and the fastest; at 0.8 outside the Focus
subtrees it sends about 11 Focus, 19 Area and 72 Field gradings per 100 papers. At 650 papers a night that is
roughly 70 Focus, 125 Area and 470 Field gradings, about $1.50 a night mostly on Anthropic credit (to be measured in
N2c). 0.9 saves 5 Area gradings per 100 papers and loses 5 points of recall; 0.7 adds 6 for one point.

Caveats: the threshold and the floor were chosen on the same labels they are scored on (two parameters, so little
room to overfit, but the provisional cohort is 0.14's output, not the owner's word); Circuits has silver labels only
(9 papers, all kept by every candidate); the stated-reasons papers are a question of the CoT faithfulness topic's
subject, not of the classifier.

## Runs

| Candidate | Papers | Failed | Cost per 1,000 | Seconds: mean / p90 / max |
| --- | --- | --- | --- | --- |
| luna-decisions | 1495 | 0% (3/1495) | $1.31 | 0.4 / 0.4 / 27.5 |
| jev | 1495 | 0% (0/1495) | $0.26 | 0.2 / 0.3 / 0.9 |
| haiku | 1495 | 0% (0/1495) | $0.69 | 4.2 / 6.3 / 10.7 |

Cost is over answered papers; failed calls were provider errors and timeouts, which are not billed.

## Focus membership recall

Of the papers each cohort holds central (0.14 and regression: tier 1–2) on a Focus topic, those that reach its class: classifier at 0.5 / with the uncertain band [0.2, 0.5) / with the band and the keyword check. Silver: both labs put the paper in the class, whether or not they agreed on its primary class.

| Cohort | Class | Positives | luna-decisions | jev | haiku |
| --- | --- | --- | --- | --- | --- |
| owner | cot-faithfulness | 5 | 5/5 / 5/5 / 5/5 | 4/5 / 5/5 / 5/5 | 5/5 / 5/5 / 5/5 |
| owner | cot-monitoring | 2 | 2/2 / 2/2 / 2/2 | 1/2 / 2/2 / 2/2 | 0/2 / 1/2 / 2/2 |
| provisional | cot-faithfulness | 39 | 18/39 / 21/39 / 34/39 | 22/39 / 30/39 / 35/39 | 24/39 / 37/39 / 38/39 |
| provisional | cot-monitoring | 51 | 47/51 / 48/51 / 49/51 | 38/51 / 51/51 / 51/51 | 24/51 / 38/51 / 41/51 |
| regression-silver | cot-faithfulness | 19 | 16/19 / 17/19 / 19/19 | 17/19 / 19/19 / 19/19 | 19/19 / 19/19 / 19/19 |
| regression-silver | cot-monitoring | 23 | 23/23 / 23/23 / 23/23 | 23/23 / 23/23 / 23/23 | 21/23 / 22/23 / 22/23 |
| regression-legacy | cot-faithfulness | 39 | 18/39 / 21/39 / 34/39 | 22/39 / 30/39 / 35/39 | 24/39 / 37/39 / 38/39 |
| regression-legacy | cot-monitoring | 52 | 48/52 / 49/52 / 50/52 | 39/52 / 52/52 / 52/52 | 25/52 / 39/52 / 42/52 |
| silver | cot-faithfulness | 5 | 4/5 / 5/5 / 5/5 | 5/5 / 5/5 / 5/5 | 5/5 / 5/5 / 5/5 |
| silver | cot-monitoring | 7 | 7/7 / 7/7 / 7/7 | 7/7 / 7/7 / 7/7 | 6/7 / 7/7 / 7/7 |
| silver | circuits | 9 | 9/9 / 9/9 / 9/9 | 9/9 / 9/9 / 9/9 | 6/9 / 9/9 / 9/9 |

## Focus volume on the 0.14 draw, by threshold

Share of the 300 drawn papers that reach any Focus class, and recall on the provisional cohort (0.14 tier 1–2 on the CoT topics, the cohort N1's check is on): classifier / with the band [0.2, threshold) / with band and keywords.

| Threshold | luna-decisions volume | luna-decisions provisional recall | jev volume | jev provisional recall | haiku volume | haiku provisional recall |
| --- | --- | --- | --- | --- | --- | --- |
| 0.3 | 1.0% / 1.3% / 4.7% | 67/90 / 69/90 / 83/90 | 1.7% / 3.0% / 6.0% | 72/90 / 81/90 / 86/90 | 1.0% / 1.7% / 4.7% | 66/90 / 75/90 / 79/90 |
| 0.4 | 1.0% / 1.3% / 4.7% | 66/90 / 69/90 / 83/90 | 1.3% / 3.0% / 6.0% | 65/90 / 81/90 / 86/90 | 0.3% / 1.7% / 4.7% | 56/90 / 75/90 / 79/90 |
| 0.5 | 1.0% / 1.3% / 4.7% | 65/90 / 69/90 / 83/90 | 0.3% / 3.0% / 6.0% | 60/90 / 81/90 / 86/90 | 0.3% / 1.7% / 4.7% | 48/90 / 75/90 / 79/90 |
| 0.6 | 1.0% / 1.3% / 4.7% | 64/90 / 69/90 / 83/90 | 0.3% / 3.0% / 6.0% | 55/90 / 81/90 / 86/90 | 0.3% / 1.7% / 4.7% | 44/90 / 75/90 / 79/90 |
| 0.7 | 0.7% / 1.3% / 4.7% | 63/90 / 69/90 / 83/90 | 0.3% / 3.0% / 6.0% | 51/90 / 81/90 / 86/90 | 0.0% / 1.7% / 4.7% | 40/90 / 75/90 / 79/90 |

## The uncertain band's floor

At threshold 0.5, with the band and the keyword check: Focus volume on the draw and provisional recall, by the band's floor.

| Floor | luna-decisions volume | luna-decisions provisional recall | jev volume | jev provisional recall | haiku volume | haiku provisional recall |
| --- | --- | --- | --- | --- | --- | --- |
| 0.1 | 5.7% | 86/90 | 9.3% | 90/90 | 7.0% | 83/90 |
| 0.2 | 4.7% | 83/90 | 6.0% | 86/90 | 4.7% | 79/90 |
| 0.3 | 4.3% | 83/90 | 5.0% | 80/90 | 4.7% | 74/90 |

## Grading on the 0.14 draw

Share of the drawn papers each followed topic would grade at 0.5 with the band at 0.2: for each direct membership, the nearest followed class at or above it (pipeline.md, section 4), the band and the keyword check included. And the mean number of direct memberships per answered paper.

| Topic | Depth | luna-decisions | jev | haiku |
| --- | --- | --- | --- | --- |
| cot-faithfulness | focus | 3.0% | 3.0% | 3.0% |
| cot-monitoring | focus | 1.0% | 1.3% | 0.7% |
| circuits | focus | 1.3% | 3.0% | 1.7% |
| interpretability | area | 13.3% | 14.3% | 5.3% |
| alignment-and-safety | area | 18.0% | 22.3% | 8.0% |
| language-models | field | 52.3% | 53.7% | 40.0% |
| reasoning-and-rl | field | 11.0% | 12.0% | 3.7% |
| agents | field | 21.7% | 22.0% | 16.7% |
| efficiency-and-architecture | field | 24.0% | 25.7% | 8.0% |
| direct memberships per paper |  | 6.0 | 4.9 | 2.0 |

## A higher threshold outside the Focus subtrees

Classes at or under a Focus topic keep 0.5; every other class takes the threshold in the row; the band's floor is 0.1. Precision / recall on the silver memberships (levels 1–2, pooled over classes); provisional recall with band and keywords; and gradings per 100 drawn papers for the Focus, Area and Field topics (a paper graded for two topics of a depth counts twice).

| Others at | luna-decisions | jev | haiku |
| --- | --- | --- | --- |
| 0.5 | 0.37 / 0.99; 86/90; 7 / 31 / 109 | 0.43 / 0.99; 90/90; 11 / 37 / 113 | 0.90 / 0.89; 83/90; 8 / 13 / 68 |
| 0.6 | 0.39 / 0.99; 86/90; 7 / 28 / 105 | 0.50 / 0.98; 90/90; 11 / 29 / 101 | 0.93 / 0.86; 83/90; 8 / 12 / 56 |
| 0.7 | 0.41 / 0.98; 86/90; 7 / 26 / 102 | 0.61 / 0.97; 90/90; 11 / 25 / 85 | 0.95 / 0.83; 83/90; 8 / 11 / 48 |
| 0.8 | 0.45 / 0.96; 86/90; 7 / 23 / 94 | 0.72 / 0.96; 90/90; 11 / 19 / 72 | 0.97 / 0.81; 83/90; 8 / 11 / 46 |
| 0.9 | 0.57 / 0.93; 86/90; 7 / 17 / 79 | 0.89 / 0.91; 90/90; 11 / 14 / 57 | 0.97 / 0.80; 83/90; 8 / 10 / 41 |

## Primary class

Exact class / same top-level class.

| Cohort | Papers | luna-decisions | jev | haiku |
| --- | --- | --- | --- | --- |
| silver | 165 | 78% (129/165) / 95% (157/165) | 75% (123/165) / 96% (158/165) | 72% (118/165) / 96% (158/165) |
| adjudicated | 20 | 15% (3/20) / 70% (14/20) | 15% (3/20) / 70% (14/20) | 30% (6/20) / 60% (12/20) |

## Classes on the silver sample (levels 1–2)

Precision / recall at 0.5, inherited memberships included, over every paper both labs sorted; a class only one lab gave the paper counts neither way. Classes with at least 3 members.

| Class | Members | luna-decisions | jev | haiku |
| --- | --- | --- | --- | --- |
| Language models | 81 | 0.66 / 1.00 | 0.69 / 1.00 | 0.80 / 1.00 |
| Instruction tuning and preference training | 3 | 0.19 / 1.00 | 0.50 / 1.00 | 1.00 / 1.00 |
| Reasoning and RL post-training | 19 | 0.45 / 1.00 | 0.40 / 1.00 | 0.90 / 1.00 |
| Agents and tool use | 25 | 0.62 / 1.00 | 0.61 / 1.00 | 0.77 / 0.96 |
| Efficiency and architecture | 18 | 0.41 / 1.00 | 0.46 / 1.00 | 1.00 / 1.00 |
| Retrieval-augmented generation | 4 | 0.57 / 1.00 | 0.67 / 1.00 | 0.75 / 0.75 |
| Multimodal language models | 8 | 0.67 / 1.00 | 0.67 / 1.00 | 0.89 / 1.00 |
| Language tasks and applications | 5 | 0.14 / 1.00 | 0.13 / 0.80 | 1.00 / 0.60 |
| Interpretability | 31 | 0.61 / 1.00 | 0.67 / 1.00 | 0.97 / 0.97 |
| Features and representations | 12 | 0.38 / 1.00 | 0.46 / 1.00 | 0.90 / 0.75 |
| Sparse autoencoders and dictionary learning | 3 | 1.00 / 1.00 | 1.00 / 1.00 | 1.00 / 1.00 |
| Circuits and causal analysis | 9 | 0.82 / 1.00 | 0.75 / 1.00 | 1.00 / 0.67 |
| Steering and model editing | 5 | 0.45 / 1.00 | 0.21 / 1.00 | 1.00 / 0.60 |
| Interpretability methodology and evaluation | 3 | 0.08 / 1.00 | 0.08 / 1.00 | 0.60 / 1.00 |
| Chain-of-thought faithfulness | 5 | 0.80 / 0.80 | 0.83 / 1.00 | 0.71 / 1.00 |
| Alignment and safety | 33 | 0.63 / 1.00 | 0.45 / 1.00 | 0.97 / 0.91 |
| Jailbreaks, red-teaming and robustness | 5 | 0.38 / 1.00 | 0.19 / 1.00 | 1.00 / 0.80 |
| Prompt injection and model security | 6 | 0.33 / 1.00 | 0.20 / 1.00 | 1.00 / 0.67 |
| Deception, sandbagging and scheming | 4 | 0.36 / 1.00 | 0.25 / 1.00 | 1.00 / 0.75 |
| Reward hacking and specification gaming | 4 | 1.00 / 1.00 | 0.33 / 1.00 | 1.00 / 0.50 |
| Scalable oversight and AI control | 4 | 0.40 / 1.00 | 0.09 / 1.00 | 1.00 / 0.50 |
| Dangerous capabilities and safety evaluations | 6 | 0.19 / 1.00 | 0.18 / 1.00 | 1.00 / 0.33 |
| Chain-of-thought monitoring | 7 | 0.54 / 1.00 | 0.64 / 1.00 | 1.00 / 0.86 |
| Machine learning beyond language models | 65 | 0.69 / 1.00 | 0.78 / 0.98 | 1.00 / 0.94 |
| Learning theory and optimization | 8 | 0.38 / 1.00 | 0.44 / 1.00 | 0.88 / 0.88 |
| Reinforcement learning | 7 | 0.50 / 0.86 | 0.78 / 1.00 | 0.88 / 1.00 |
| Generative models for images, video and audio | 5 | 0.60 / 0.60 | 0.67 / 0.80 | 1.00 / 0.80 |
| Computer vision | 5 | 0.28 / 1.00 | 0.31 / 1.00 | 1.00 / 0.80 |
| Graph and geometric learning | 6 | 0.67 / 1.00 | 0.75 / 1.00 | 1.00 / 1.00 |
| Time series and tabular data | 8 | 0.73 / 1.00 | 0.89 / 1.00 | 1.00 / 0.50 |
| Probabilistic and causal inference | 4 | 0.40 / 1.00 | 0.36 / 1.00 | 1.00 / 0.50 |
| Federated and privacy-preserving learning | 3 | 1.00 / 1.00 | 1.00 / 1.00 | 1.00 / 1.00 |
| Robotics and embodied AI | 5 | 0.62 / 1.00 | 0.56 / 1.00 | 1.00 / 0.80 |
| AI for science and health | 16 | 0.53 / 1.00 | 0.84 / 1.00 | 1.00 / 0.81 |
| Classical AI, planning and logic | 3 | 0.43 / 1.00 | 1.00 / 1.00 | 1.00 / 1.00 |

## 0.14's selections on the draw

Precision / recall at 0.5 against 0.14's tiers: tier 1–2 a member, tier 4 not, tier 3 and unscreened left out.

| Selection | Members | luna-decisions | jev | haiku |
| --- | --- | --- | --- | --- |
| interpretability | 7 | 0.37 / 1.00 | 0.28 / 1.00 | 0.88 / 1.00 |
| alignment-and-safety | 19 | 0.83 / 1.00 | 0.68 / 1.00 | 1.00 / 0.89 |
| reasoning-and-rl | 8 | 0.67 / 1.00 | 0.57 / 1.00 | 1.00 / 0.62 |
| agents | 30 | 0.88 / 1.00 | 0.81 / 0.97 | 0.97 / 0.97 |
| efficiency-and-architecture | 17 | 0.39 / 1.00 | 0.39 / 1.00 | 1.00 / 0.88 |
