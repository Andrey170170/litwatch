# Failure handling

Approved by the owner on 2026-10-19. This is the reference for how v1 sorts its failures: what reaches the owner's
monitoring (Vergil) and what stays in the app, the fallback models each role asks when its own model fails, Deep cards
without a daily cap, and Acknowledge on Needs you. What litwatch does is the spec, [`docs/specs/v1/`](../specs/v1/README.md);
this plan amended [models.md](../specs/v1/models.md) (fallback lists, lab diversity),
[pipeline.md](../specs/v1/pipeline.md) (sections 4, 6, 9 and 10), [digests.md](../specs/v1/digests.md) (the
wait's terminal passes), [integrations.md](../specs/v1/integrations.md) (deep),
[data.md](../specs/v1/data.md) (configuration, the status document), [DESIGN.md](../specs/v1/DESIGN.md) (the run
status, Needs you) and [decisions.md](../specs/v1/decisions.md) (68–72) and [milestones.md](../specs/v1/milestones.md). Where this plan and the spec differ, the spec
wins and this plan is corrected. Terms are the glossary's.

## Goal and approach

On 2026-10-19 Vergil pushed "Litwatch degraded: papers stopped: 2 ungraded". `judge` (Opus 5.5) had refused two
biosecurity papers (2610.13116, 2610.21200) three nights running; any stuck paper turns the status document's
`pipeline` component degraded. Meanwhile no new paper had a Deep card for days: `max_per_day` (10 automatic
submissions in 24 hours) was draining a backlog of older papers first.

The principle the owner set: **Vergil is for "litwatch can't do its job"; Needs you is for "a paper needs your
word".** A failure's reason decides where it goes. A paper's own failure (every model refused it, unusable answers,
no text) stays on Needs you and in the run status. A failure that stops litwatch working (an expired sign-in or key,
used-up credits, a provider down all night, many papers failing at once) goes to Vergil. Vergil's own pushes (a
container down, and so on) are unchanged; litwatch's status document adds to them.

Around that:

- every role that writes gets an **ordered list of fallback models**, asked on refusals at once and on repeated
  unusable answers one step per run, chosen by a replay;
- **Deep cards lose their daily cap**; a Deep job that fails because of the service no longer counts against the
  paper, and pauses the queue instead;
- **Acknowledge** on Needs you clears the run status's "N failed" once the owner has seen the items;
- **backfill gives way to the nightly**: work a re-run queues is background.

## Where a failure goes

| Reason | Counts against the paper | Where it shows |
| --- | --- | --- |
| Every model in the role's list refused | yes, and the step stops at once | Needs you |
| Invalid or truncated answer, three runs | yes | Needs you |
| No text to be had; a Deep job failed because of the paper | yes | Needs you |
| Many papers stopped at one step in one night | – | Vergil: `pipeline` |
| Provider 401, 402 or 403 | no | Vergil at once: `provider-<name>` |
| Provider timeouts or 5xx errors, no answered call all night | no | Vergil: `provider-<name>` |
| Deep sign-in, usage limit or service down | no; the queue pauses | Vergil: `deep` |
| Nightly failed or late; archive down; integration stuck; digest failed; budget over | – | Vergil (unchanged) |

## Choices made without asking

### Fallback models

- **Configuration.** A role's `fallback` (one role name) becomes `fallbacks`: an ordered list of models, each with
  its provider, model and effort, written inline on the role. The `brief_fallback` role goes. Calls to a fallback
  model are journalled under the role's own name, with the model recorded on each call as today.
- **What moves down the list.** A refusal goes to the next model at once, in the same run. An unusable answer
  (invalid or truncated) still counts as one of the paper's three failed runs and moves down one step per failed run
  (D4). A provider error or timeout never moves down: it says nothing about the paper, it isn't counted, and if it
  lasts a night it goes to Vergil.
- **`writer` has no three-run stop**: digests are retried every run until written. Each failed digest of the topic
  in a row starts one model further down, and the last model is kept until a digest of the topic is published.
- **A Focus side refused while the other hasn't answered** (a timeout, say) keeps the grading pending; the refused
  side isn't asked again, and the one-lab rule applies when the other answers.
- **Answers are reused.** An answer any model in the list has given is reused, as the single fallback's is today.
  Asked again, the role starts from the model that answered.
- **A refusal stops at once.** When every model in the list refuses, the step stops that night and goes straight to
  Needs you, without three nights of asking the same models again.
- **No backfill on its own.** The new rules apply to papers processed from now on. A paper already stopped gets them
  through Grade it now, Try again, or a re-run the owner starts.
- **Only Focus has a one-lab grade.** An Area grading whose `judge` list, or whose escalation's `check` list, refused
  throughout stops ungraded like any other step; so does a Field grading.
- **The owner's retry starts from the top.** Try again, Grade it now and a re-run ask the role's list from its own
  model, with three more runs as today.
- **Decision roles are out.** The lists cover the roles that write: `analyse`, `judge`, `check`, `field_grade`,
  `brief` and `writer`. `classify` and `area_check` answer through OpenRouter's Decisions endpoint with probabilities
  and have never refused.

### Starting lists, for the replay (P1) to keep or change

Prices per million tokens, input/output, from OpenRouter's catalogue on 2026-10-19.

| Role | Model now | Then, in order | Why |
| --- | --- | --- | --- |
| `judge` | Opus 5.5 | Sonnet 5.5 ($2/$10) → GPT-6.1 Sol ($2/$10) → Kimi K3 ($0.24/$11.94) | Sonnet answered all 3 papers Opus refused; Sol at the owner's suggestion; K3, open weights, last. Labs: Anthropic, OpenAI, Moonshot |
| `check` | Muse Spark 1.3 (Meta) | DeepSeek V4.1 Flash, the 1017 snapshot ($0.15/$0.60) → Gemini 3.8 Flash ($0.83/$4.12). The replay also tries DeepSeek V4 Pro ($0.66/$1.98) and MiniMax M3.1 Flash (free tier only on OpenRouter today, so rate-limited) | No lab in common with `judge`'s list (D2). A pinned snapshot keeps answers reproducible |
| `analyse`, `field_grade` | Haiku 5.5 | GPT-6 Luna ($0.10/$0.50) → DeepSeek V4.1 Flash ($0.033/$0.08) | Haiku's price class |
| `brief` | Sonnet 5.5, high effort | GPT-6.1 Sol, high effort (today's fallback) → Kimi K3 | Long full texts; K3 holds 1M tokens |
| `writer` | Opus 5.5 | Sonnet 5.5 → Kimi K3 | K3 for prose, at the owner's suggestion |

### Service failures and Vergil

- **Provider account errors.** HTTP 401, 402 and 403 become a new error kind, `account`. They are never retried and
  never count against a paper, in normal and batched calls alike.
- **A provider failed for its account** from an account error until its next reply (any answer it sent, a refusal or
  an invalid one included; not an answer reused from the journal).
- **A provider failed for the night** when, in the latest nightly run that has ended, it had failed calls (provider
  errors or timeouts) and not one reply.
- **Many papers stopped at once:** at one step (classification, analysis, grading, the Brief and Weight passes), more
  distinct papers became terminal during the latest ended nightly, digest wait included, than max(5, 2 % of the papers
  it classified). This is the only thing that now turns `pipeline` degraded; one stuck paper never does. Deep passes
  are not counted; `deep` covers their service failures.
- **Deep tells the two kinds apart.** The Deep service tags each failed job `service` (the model call failed:
  sign-in, usage limit, auth, the provider) or `paper` (no card submitted within the budget, an invalid card). An
  error submitting or following a job, seen from litwatch, is a service failure.
- **A service failure doesn't count.** The pass goes back to waiting, and the Deep worker pauses the whole queue for
  15 minutes before trying again, instead of moving on to the next paper. After an error following a job already
  accepted, the pass keeps its attempt and stays running, as it does today, and the next claim follows the job.
- **Submissions are idempotent.** A submission carries its attempt as a key, and the Deep service answers a key it
  already has with that key's job. A submission whose reply was lost keeps its attempt and is sent again after the
  pause, so a lost `202` can't start a second hour-long job.
- **`max_per_day` is removed**, the option and the cap. The owner's requests still go first.
- **The `deep` component reads `/healthz`.** It is degraded while the sign-in state there is not `ok` or the check
  doesn't answer, and from any service failure litwatch sees until the next card is made.
- **Failed digests still push**, whatever the reason: a day without its digest is litwatch not doing its job.

### Acknowledge

- **What gets the mark.** An acknowledged mark on each stuck-step record, each full-text pass waiting for the owner,
  and each failed digest. Acknowledge on a group sets it on what is in the group now; "Acknowledge all" on everything.
- An item that recovers and later fails again comes back unacknowledged.
- **The run status counts only unacknowledged failures**: stopped papers and passes, failed digests, a failed
  nightly. Splits and one-lab grades wait on Needs you but aren't counted, as in the code today (decision 14 is
  marked superseded). With no failure left it shows the night's papers again ("530", cyan).
- Acknowledged items move to a folded "Acknowledged" section at the foot of Needs you and keep their buttons.
- Only paper and digest items are acknowledged. A failed nightly is not: it clears with the next good run.

### Backfill and the nightly

Work a re-run queues is background (the owner's R5 note), written into pipeline.md section 10 now and built with
scoped re-runs, whose jobs API is still deferred in [v1-build.md](v1-build.md). Its Deep and full-text passes queue
behind the nightly's own, at a priority after the automatic ones, and a re-run still going at 02:00 yields its model
calls to the nightly until the night's papers are done.

## Decisions

- **D1. A fallback may come from the same lab as the model that refused: allowed, listed explicitly.** The start-up
  rule that a fallback must come from another lab goes. Each list is written out in order, so Sonnet can follow
  Opus. The evidence: Sonnet answered all 3 papers Opus refused
  ([evals/2026-10-16-sonnet-briefs.md](../../evals/2026-10-16-sonnet-briefs.md)). The cost: the rule's guarantee
  goes, and the replay has to show a same-lab model actually helps.
- **D2. A Focus grade keeps two labs by fixed lists: `judge`'s and `check`'s lists share no lab.** Start-up refuses a
  configuration where the two lists, each including its own model, have a lab in common, as today's rule does for the
  two models alone. No logic at call time (the alternative, skipping a model at call time by the lab the other grader
  answered with, would make two concurrent graders wait on each other). The cost: fewer models open to each list.
- **D3. A Focus grade from one lab gets its own Needs you group, "Graded by one lab",** when it is central or related.
  Yellow, settled with the Fit control like splits, with one line saying which grader refused and that only the other
  answered. The verdict gets a `one_lab` mark beside `split` and `escalated`. Not folded into Focus splits: a split is
  two answers that disagree, and here there is one. If both sides refuse the whole way down, the paper is ungraded and
  the owner grades it.
- **D4. Unusable answers move one step down per failed run.** The first run asks the role's model; each run the step
  fails again starts one model further down, so the second run asks the next model and the third the one after. One
  call per run, as many as today; only the model changes. The cost: a paper the next model would have handled waits
  a night. The alternative, the next model in the same run, could spend one full-price call per model on every paper
  if a prompt or an answer check broke.

## Risks

- **R1. A fallback model could lower quality.** Fallbacks also answer after unusable answers (D4), not only after
  refusals, so their quality matters on ordinary papers. P1 measures each candidate on ordinary papers the owner
  labelled, against the owner's labels and against the role's own model; a separate small set of papers likely to be
  refused checks that the lists answer. A model that does clearly worse on the ordinary set is dropped, whatever it
  does on refusals.
- **R2. A paper's failure taken for a service failure would never count**, and the paper could be tried forever. But
  a service failure pauses the whole Deep queue and goes to Vergil, so the mistake shows within a night; it can't
  loop quietly.
- **R3. A grade from one lab is weaker than one from two.** It happens only on papers one side refused entirely, it is
  marked `one_lab`, and the ones that matter (central or related) go to the owner (D3).
- **R4. Account errors also come from batches.** An Anthropic batch request that gets a 401 must map to `account`
  too, or a revoked key at 02:00 would read as a provider outage. P3 covers the batch path, with a test.
- **R5. Without the cap, nothing bounds the number of cards but the papers that qualify.** A re-run after a topic edit
  happens only when the owner starts it (pipeline.md section 10), and its passes are background work behind the
  night's, so a large re-run takes time but never holds back new papers. New papers get cards as they qualify, and
  the owner's requests still go first.

## Stages

One PR each, in order; P5 depends on nothing else and can be built alongside P1–P4.

| Stage | Work | Shows before the next starts |
| --- | --- | --- |
| P1 | **Replay for the fallback lists.** Two sets. Quality: ordinary papers the owner labelled (Calibrate sessions and the evaluation labels in `evals/`), each role's real request sent to every candidate. Refusals: the 3 known refusals (2610.13116 and 2610.21200 for `judge`, 2610.17907 for `brief`) plus about 20 papers from the archive on biosecurity, chemical weapons, cyber offence and jailbreaks. A dry run, at most $25. | A report, `evals/2026-10-xx-fallback-ladders.md`: for each model and role, agreement with the owner's labels and with the role's own model, unusable answers, refusals and cost. It ends with the lists P2 uses, and the fallback answers on the refused papers for the owner to read. |
| P2 | **Fallback lists in the gateway.** `fallbacks` in the configuration and the start-up rules (D1, D2); refusals stop at once; D4's step down; Focus grades from one lab and their Needs you group (D3); P1's lists as the defaults. | V1, V8, V2, and V3 on the sandbox. |
| P3 | **Service failures and paper failures.** The `account` error kind, batch path included; the Deep service tags its failures and takes an idempotency key per submission; litwatch counts only paper failures and pauses the queue on service failures; `max_per_day` removed. | V4 and V5, with a real run against a dev Deep instance whose sign-in file is broken. |
| P4 | **The status document.** `pipeline` degraded only when many papers stop at once; `provider-<name>` components; `deep` reads its health check and its latest job. | V6, and `/status` read on the sandbox with the two stuck papers: `pipeline` healthy. |
| P5 | **Acknowledge.** A migration, the API, Needs you and the run status. | V7. |

## Verification

By the methods in [docs/agents/verification.md](../agents/verification.md).

| Claim | Evidence |
| --- | --- |
| V1. A refusal goes down the role's list in order, each answer is journalled and reused, the step stops at once when every model refuses, and start-up refuses lists that break D1 or D2 | pytest with faked providers (`tests/test_llm.py`, the configuration tests) |
| V8. An unusable answer moves down the list as D4 says, with no more calls per paper than D4 allows, and a provider error or timeout never moves down | pytest with faked providers, counting the calls journalled for each paper |
| V2. In a Focus grading, if every `judge` model refuses and `check` answers central, the grade stands as `one_lab` and the paper is listed on Needs you; if `check` times out instead, the grading stays pending without asking `judge` again; if both sides refuse, it is ungraded and stops at once. A Brief or Weight pass every model refused is terminal for the digest wait | pytest on real PostgreSQL (the grading tests) |
| V3. The papers refused so far get graded, and their Brief written | The P1 report, and a nightly on a sandbox copied from production over 2610.13116, 2610.21200 and 2610.17907, read back through the API |
| V4. A 401 or 402 counts against no paper and turns that provider's component degraded, in normal and batched calls alike | pytest with faked SDK errors; one real call with an invalid key (`litwatch ping`) |
| V5. With no cap every due Deep pass is worked; a service failure counts against no paper and pauses the queue; a paper failure counts; a submission retried with the same key gets the first job back | pytest with the Deep service faked over HTTP; `npm test` in `deep/` for the tags; a dev Deep instance with a broken sign-in file reports `service` |
| V6. Each push case in the table above turns its component degraded, and a single stuck paper doesn't | pytest on the status document, one case per row |
| V7. Acknowledge clears the run status, keeps the items' buttons in the folded section, and a new failure raises the count again | API tests; Needs you walked on the sandbox at 390 and 1440 px, dark and light, screenshots before and after |

## Scope

In: everything above — fallback lists for the six roles that write, refusals stopping at once, Focus grades from one
lab, the `account` error kind, Deep's two kinds of failure, no Deep cap, the status components, Acknowledge, and the
spec amendments.

Out, and where it goes instead:

- Updated papers in digests: a plan of its own, next.
- The paper page's Brief/abstract switch and topic-row fold: its own PR (#87).
- Hard-to-read model prose: the owner's separate thread.
- Fallbacks for `classify` and `area_check`: no refusals seen.
- Fallbacks inside the Deep service: its refusals are the paper's failures.
- Building the backfill priority: with scoped re-runs (v1-build.md, deferred).
- Vergil's own settings: unchanged; it reads the status document.
