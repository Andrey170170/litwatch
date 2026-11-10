# What production still uses

Read-only inventory, 2026-11-10. Recommendations for A0 P2; **drop means proposed for the owner's mark, not approved for removal** (A0 D2/R2). No code, configuration, migrations or production state were changed. Only this note was written in the repository.

## Owner to confirm

Each question takes yes or no; yes retains the named surface. Where a line names a pair, the commands implement one workflow. Manual commands remain separate rows in the inventory even when their question is grouped.

- Q1. Do your scripts use `GET /api/bibtex` to export a whole filtered collection?
- Q2. Do your scripts use `DELETE /api/papers/{paper_id}/publication` to forget a publication override?
- Q3. Do you use `POST /api/lookup` outside litwatch's web app?
- Q4. Do you use `GET /api/status` (separately from the monitored `/status` and the web's `/api/summary`)?
- Q5. Do you have a client polling `GET /api/events`?
- Q6. Does a client fetch the live `GET /openapi.json`, rather than use the schema generated from the CLI?
- Q7. Do you still need manual intake/recovery commands: `intake`, `nightly`, `digests write`, `library sync`, `publications`, `fulltext`?
- Q8. Do you still use `taxonomy export` and `taxonomy check` in a terminal?
- Q9. Do you still need `eval classify` / `eval classify-report` for future classifier comparisons?
- Q10. Do you still need `eval briefs` / `eval briefs-report` for future Brief comparisons?
- Q11. Do you still need `eval ladders` / `eval ladders-report` for future fallback comparisons?
- Q12. Do you still need terminal `eval replay` / `eval report`, alongside the web's Run replay?
- Q13. Do you still run the terminal voice check, `eval digests`?
- Q14. Do you still need the paid connectivity check, `ping`?
- Q15. Should the current Zotero grade-sync integration remain supported while Save to Zotero is shaped?
- Q16. Should the current WebDAV PDF integration remain supported?
- Q17. Should the generic webhook integration remain supported for other installs or future receivers?

## The owner's marks (2026-11-10)

- **Removed in A0's P3:** `GET /api/bibtex`, `DELETE /api/papers/{paper_id}/publication`, `POST /api/lookup`,
  `GET /api/status` and the live `/openapi.json` (Q1–Q4, Q6: no caller outside the tests); the tables
  `eval_samples` and `regression`, the old `eval_results` columns `triage_yes`, `triage_why` and `grade`,
  `eval_runs.run_id`, and the label origins `candidate_sample`, `reject_sample`, `legacy_positive` and `gold`.
- **Removed in A1,** whose rewrite of `store` and settling covers them: the live-label `provisional` handling and the
  revision-1 fallback.
- **Kept:** `GET /api/events` (Q5), the integrations' changes feed for a polling client; every CLI command (Q7–Q14);
  the Zotero and WebDAV integrations (Q15, Q16), which Save to Zotero will build on; the webhook (Q17).

## Scope and evidence

The requested baseline is `main` at `cc3b3f641432280e56638e62ef8bbb410c0b0082`. The shared checkout was already on `docs/a0-plan` at `e58929383f73b5f543a711dee3b8be9e1d328fcd`; `git diff main -- src tests web deep deploy scripts` was empty. I did not switch branches. Its differences from main were the A0 plan and backlog text; the pre-existing untracked `docs/notes/where-work-waits.md` was left alone. The plan was read with `git show docs/a0-plan:docs/plans/a0-rules-baseline.md` (P2, D2, R2, V3–V4).

Production is image `litwatch:1.0.0-dev-fe1cefd`, OCI revision `fe1cefd49df02490db9796d284468fb3c609f10d`, with migrations 1–41 applied (latest 2026-11-09 20:51 UTC). Config, evaluation code, and the non-Deep findings below match that deployed revision. Main's additional migrations 42–43 and its newer Deep endpoints are outside the production-use inference; their callers are recorded only at the API boundary. `git diff --stat fe1cefd main -- src/litwatch/config.py src/litwatch/cli.py src/litwatch/api src/litwatch/quality src/litwatch/core/types.py src/litwatch/store` established that difference.

Production aggregates were sampled at **17:43:48 UTC** and **17:51:40 UTC**. The journal begins on 2026-10-17; its entire surviving history fits in the last 30 days. “All time” below means surviving rows, not an assertion that no data was ever deleted. Empty tables mean no surviving use; they do not establish a last-write time. Evaluation children have no timestamp column, so their last write is unobservable independently of their parent; the parent table is empty too.

All substantive data queries ran through `docker exec -i litwatch-db psql -X -U litwatch -d litwatch`, with `SET default_transaction_read_only = on`, a 20-second statement timeout, and an explicit repeatable-read **READ ONLY** transaction; `current_setting('transaction_read_only')` returned `on`. The initial schema-discovery call put SET and SELECT in one `-c` string, which left that one implicit transaction's read-only flag `off`; it contained only SELECTs, changed no data, and was replaced by the explicitly read-only evidence sessions. No application command or tests were run. No `.env` files, secret files or container environments were read. The production TOML was read at the compose-documented `/etc/litwatch/litwatch.toml` and filtered before any output or scratch persistence. Credential keys and credential-bearing sections were excluded.

Scratch scripts, aggregate results and the filtered TOML are in `/tmp/litwatch-still-used/`; the decisive queries are reproduced below so the note does not depend on scratch persistence. `/tmp/litwatch-waits/` and `litwatch-waits-20261109-final.service` were not touched.

Archive implementation, Deep implementation and the already planned pre-OAI transport, Deep dispatcher registration and web scaffold removals are excluded. Archive/Deep appear only as config or API caller boundaries. No workload was started, stopped or reconfigured.

## Configuration

Inventory unit: each leaf field defined by `config.py`, with `models.<role>` counted once per role and the eight `RoleConfig` fields counted once as a reusable schema (also covering every fallback). The eight nested section containers inherit the decisions of their leaves. This covers all definitions, rather than treating each fallback's identical schema as another option.

**No defined option lacked a reader, and no inspected non-secret production key was stale.** The inspected non-secret production settings are intake categories; schedule enabled/nightly/timezone/deadline; MCP allowed hosts; archive harvest; the Deep section; and Notion parent_page, owner_name, minimum_grade and topics. The loader consumes the integration mapping (`integrations/__init__.py:61`); archive reads `harvest` (`integrations/archive.py:224`), and Notion reads all four of its keys (`integrations/notion.py:1059`). Production's `debug` file default is known; whether `LITWATCH_DEBUG` overrides it was deliberately not inspected.

“Default only” below means production's file and all **tracked TOML configurations** inspected use the definition's default, or omit the key. It does not claim that tests, environment overrides or other installations never change it. The eight scalar tuning keys marked D and the three role fields marked D have no explicit assignment in tracked TOMLs. They are active policy/limit settings; retaining their configurability is cheap. Default-only use is not evidence of dead code.

Searches: `rg -n 'config\.(debug|intake|models|classification|grading|digests|budget|schedule|mcp|integrations)' src/litwatch --glob '*.py'`; `rg -n 'role\.(provider|model|effort|routing|timeout_seconds|max_output_tokens|decisions|fallbacks)' src/litwatch/llm`; source-level following of the `thresholds`, `by_provider`, `models`, `zone` and `night` properties. For TOML assignments, `git ls-files '*.toml'` plus `tomllib` over those tracked files only, excluding `archive/` and `deep/`. Test overrides were searched with `rg -n 'window_days|area_check_min_confidence|anthropic_monthly|openrouter_monthly|wait_minutes|timeout_seconds|max_output_tokens|routing' tests --glob '*.py'`.

| Item | Decision | Evidence | Confidence |
| --- | --- | --- | --- |
| `debug` | keep | File default false, environment override unverified; config.py:313,336 → jobs/context.py:28 → llm/gateway.py:420 (failed-reply capture). | medium |
| `intake.categories` | keep | Explicit [cs.CL, cs.AI, cs.LG], equal to default; tests/stack/litwatch.toml:6 narrows to cs.CL. config.py:179 → jobs/intake.py:45; nightly.py:211. | high |
| `intake.window_days` | keep | D: 3; config.py:181 → jobs/intake.py:41 and jobs/nightly.py:201. | high |
| `classification.default_threshold` | keep | D: 0.8; config.py:188 → Classification.thresholds:199 → jobs/nightly.py:51, quality/replay.py:376, api/settings.py:73; policies core/classification.py:69,88. | high |
| `classification.focus_threshold` | keep | D: 0.5; config.py:189 → Classification.thresholds:199 → jobs/nightly.py:51, quality/replay.py:376, api/settings.py:73; policies core/classification.py:69,88. | high |
| `classification.uncertain_floor` | keep | D: 0.1; config.py:190 → Classification.thresholds:199 → jobs/nightly.py:51, quality/replay.py:376, api/settings.py:73; policies core/classification.py:69,88. | high |
| `grading.area_check_min_confidence` | keep | D: 0.7; config.py:209 → grading/__init__.py:184 (Area escalation). | high |
| `digests.wait_minutes` | keep | D: 120; config.py:216 → jobs/nightly.py:257 (full-text wait). | high |
| `budget.anthropic_monthly` | keep | D: USD 100; config.py:224 → Budget.by_provider:228 → api/deps.py:83 → summary/status spend. | high |
| `budget.openrouter_monthly` | keep | D: USD 25; config.py:225 → same live budget reader. | high |
| `schedule.enabled` | keep | Explicit true, default false; config.py:256 → api/app.py:42,64 (workers), jobs/scheduler.py; Q3 has 25 nightly runs. | high |
| `schedule.nightly` | keep | Explicit 0 2 * * *, equal to default; config.py:258 → jobs/scheduler.py:79, worker.py:65; test stack overrides cron. | high |
| `schedule.publications` | keep | File default 30 9 * * *; config.py:260 → jobs/scheduler.py:80. Test stack overrides; Q3 has 25 publication runs. | high |
| `schedule.deadline` | keep | Explicit 07:30, equal to default; config.py:263 → Schedule.night:295 → jobs/nightly.py:202 and api/monitor.py:381. | high |
| `schedule.timezone` | keep | Explicit America/Denver, default server zone; deploy/litwatch.toml sets UTC. config.py:265 → zone:287 → scheduler.py:125, api/digests.py:128. | high |
| `mcp.allowed_hosts` | keep | Explicit [the public host name], replaces local-only default; config.py:307 → api/app.py:150 → api/mcp.py:182 (Host validation). | high |
| `integrations` | keep | Explicit archive {harvest=true}, deep enabled (implementation excluded), notion {parent_page UUID configured, owner_name set, minimum_grade=relevant, topics: cot-faithfulness=CoT faithfulness, cot-monitoring=CoT monitoring, circuits=Circuits}; no Zotero/WebDAV/webhook sections. config.py:322 → integrations/__init__.py:61; each selected loader reads its keys. Deep/archive internals excluded. | high |
| `models.classify` | keep | Entire role uses config.py:100 defaults; none set in production TOML. Models.role:173 → llm/gateway.py:287,319. Nonzero production calls in Q1; exact model and ladder listed in the roles table. | high |
| `models.analyse` | keep | Entire role uses config.py:103 defaults; none set in production TOML. Models.role:173 → llm/gateway.py:287,319. Nonzero production calls in Q1; exact model and ladder listed in the roles table. | high |
| `models.judge` | keep | Entire role uses config.py:111 defaults; none set in production TOML. Models.role:173 → llm/gateway.py:287,319. Nonzero production calls in Q1; exact model and ladder listed in the roles table. | high |
| `models.check` | keep | Entire role uses config.py:121 defaults; none set in production TOML. Models.role:173 → llm/gateway.py:287,319. Nonzero production calls in Q1; exact model and ladder listed in the roles table. | high |
| `models.area_check` | keep | Entire role uses config.py:131 defaults; none set in production TOML. Models.role:173 → llm/gateway.py:287,319. Nonzero production calls in Q1; exact model and ladder listed in the roles table. | high |
| `models.field_grade` | keep | Entire role uses config.py:134 defaults; none set in production TOML. Models.role:173 → llm/gateway.py:287,319. Nonzero production calls in Q1; exact model and ladder listed in the roles table. | high |
| `models.brief` | keep | Entire role uses config.py:137 defaults; none set in production TOML. Models.role:173 → llm/gateway.py:287,319. Nonzero production calls in Q1; exact model and ladder listed in the roles table. | high |
| `models.writer` | keep | Entire role uses config.py:143 defaults; none set in production TOML. Models.role:173 → llm/gateway.py:287,319. Nonzero production calls in Q1; exact model and ladder listed in the roles table. | high |
| `models.<role or fallback>.provider` | keep | config.py:41. Required per role; all primary/fallback defaults provide it. llm/gateway.py:192 and both provider adapters select the account. | high |
| `models.<role or fallback>.model` | keep | config.py:42. Required per role; llm/anthropic.py:40, openrouter.py:65 and gateway.py:202 read it; evaluation TOMLs supply alternatives. | high |
| `models.<role or fallback>.effort` | keep | config.py:43. Per-role defaults None/medium/high; llm/anthropic.py:37 and openrouter.py:62. Evaluation configs override it. | high |
| `models.<role or fallback>.routing` | keep | config.py:46. D: empty tuple; openrouter.py:217 expands it to DEFAULT_ROUTING by model, then request builders consume it. Empty is an active fallback rule. | high |
| `models.<role or fallback>.timeout_seconds` | keep | config.py:47. D: 180 seconds; llm/gateway.py:228 wraps each provider call in asyncio.timeout. | high |
| `models.<role or fallback>.max_output_tokens` | keep | config.py:48. D: None; llm/anthropic.py:41, openrouter.py:80, gateway.py:262,478 combine it with request/candidate bounds and built-in limits. | high |
| `models.<role or fallback>.decisions` | keep | config.py:51. True by default for classify/area_check, false for chat roles/fallbacks; llm/gateway.py:208,290 and config.py:58,168 select and validate the endpoint. | high |
| `models.<role or fallback>.fallbacks` | keep | config.py:54. Nonempty default ladders for five roles; config.py:74 exposes Models, llm/gateway.py:319,347 walks the ladder. Decision-role fallback prohibition is active validation. | high |

## Model roles

Q1 groups `inference_calls.operation`, the role persisted by `store/calls.py`; count is journal records, including attempts that failed, not a count of successful papers. Eight configured roles and eight observed operations agree exactly. Role constants in `quality/ladders.py:68` (six chat roles) and `quality/replay.py:54` (classify/judge/check) are subsets, not additional dormant roles. Deep accounts its own models and is excluded.

Unless a repository prefix is shown, package/file locations in the configuration, roles and evaluation tables are under `src/litwatch/`. All timestamps below are 2026-11-10 UTC. Production has no `[models]` overrides; defaults shown include every fallback.

| Item | Decision | Evidence | Confidence |
| --- | --- | --- | --- |
| `classify` | keep | Q1: **19,384 / 19,384** calls (30 days / all time), latest 09:04:52. `openrouter:typesafe/jev-1.13, Decisions; no fallback`. Entry: `src/litwatch/classification/__init__.py:98`. | high |
| `analyse` | keep | Q1: **19,391 / 19,391** calls (30 days / all time), latest 09:14:13. `anthropic:claude-haiku-5-5 → openrouter:openai/gpt-6-luna → openrouter:deepseek/deepseek-v4.1-flash`. Entry: `src/litwatch/analysis/__init__.py:120`. | high |
| `judge` | keep | Q1: **6,171 / 6,171** calls (30 days / all time), latest 09:23:13. `anthropic:claude-opus-5-5@medium → anthropic:claude-sonnet-5-5@medium → openrouter:openai/gpt-6.1-sol@medium → openrouter:moonshotai/kimi-k3@medium`. Entry: `src/litwatch/grading/__init__.py:160,181`. | high |
| `check` | keep | Q1: **2,866 / 2,866** calls (30 days / all time), latest 09:23:15. `openrouter:meta/muse-spark-1.3-contributor@high → openrouter:deepseek/deepseek-v4.1-flash-1017@high → openrouter:google/gemini-3.8-flash@high`. Entry: `src/litwatch/grading/__init__.py:160,188`. | high |
| `area_check` | keep | Q1: **3,343 / 3,343** calls (30 days / all time), latest 09:23:13. `openrouter:typesafe/jev-1.13, Decisions; no fallback`. Entry: `src/litwatch/grading/__init__.py:131`. | high |
| `field_grade` | keep | Q1: **14,833 / 14,833** calls (30 days / all time), latest 09:14:43. `anthropic:claude-haiku-5-5; no fallback`. Entry: `src/litwatch/grading/__init__.py:192`. | high |
| `brief` | keep | Q1: **1,281 / 1,281** calls (30 days / all time), latest 09:33:32. `anthropic:claude-sonnet-5-5@high → openrouter:openai/gpt-6.1-sol@high`. Entry: `src/litwatch/fulltext/brief.py:19, fulltext/__init__.py:121`. | high |
| `writer` | keep | Q1: **145 / 145** calls (30 days / all time), latest 09:40:48. `anthropic:claude-opus-5-5@medium → anthropic:claude-sonnet-5-5@medium → openrouter:moonshotai/kimi-k3@medium`. Entry: `src/litwatch/digests/__init__.py:103`. | high |

## Evaluation tables and cohorts

Q2: `eval_runs`, `eval_results`, `eval_labels`, `eval_samples` and `regression` each contain **0 rows**. `max(eval_runs.started_at)` and `max(finished_at)` are NULL; no kind/cohort/source groups exist. Q9 confirms no timestamp column on the four child/legacy tables, so no child-specific last write can be supplied. Q3: `calibration_sessions` and `calibration_items` also contain 0 rows (max drawn/answered time NULL). Their maintained UI writers matter more than this lack of activity.

Searches: `rg -n 'eval_|regression|cohort' src/litwatch/{quality,store,cli.py,jobs,api} --glob '*.py'`; `rg -n 'eval_samples|\bregression\b' src tests --glob '*.py' --glob '*.sql'`. The only SQL reference to `regression` is its creation; CSV readers named regression are a different source. `eval_samples` is likewise only created. In contrast, replay is offered by the web (`queries.ts:213`, `mutations.ts:505`), implemented by `jobs/replays.py`, and its recall is read by Overview (`store/quality.py:105`).

| Item | Decision | Evidence | Confidence |
| --- | --- | --- | --- |
| `eval_runs` | keep | 0; timestamps NULL (Q2). Written by store/evaluation.py:16,56,66 (start, associate resumed runs, finish), called by quality/replay.py:290, jobs/replays.py:make/work and CLI replay. Read by evaluation.get/latest → replay.resume/report, store/quality.py:105. A2 submits evaluations; A4 owns eval tooling. | high |
| `eval_results` | keep | 0; last write unavailable (Q2/Q9). evaluation.py:105,144 writes classified/graded outcomes; :97 forgets stale results. :83,182,206 reads pairs/outcomes/results for resume, metrics and reports. Obsolete columns are a separate guard finding below. | high |
| `eval_labels` | keep | 0; last write unavailable (Q2/Q9). evaluation.py:71 freezes labels at replay start; :83,182 reads them for resume and frozen metrics. Removing it would make old results depend on edited label files. | high |
| `eval_samples` | drop | 0; last write unavailable (Q2/Q9). Only source/test match is CREATE TABLE at store/migrations/0002_watch.sql:145. No current writer or reader. Actual Calibrate uses calibration_items. Removal D1 below. | high |
| SQL `regression` table | drop | 0; last write unavailable (Q2/Q9). Only SQL match is CREATE TABLE at store/migrations/0002_watch.sql:106. No current writer or reader; quality/replay.py:78 and quality/classify.py:80 read evals/labels/regression.csv instead. Removal D1 below. | high |
| `calibration_sessions` | keep | 0; drawn_at max NULL (Q3). feedback/calibrate.py:draw/session/sessions creates and reads sessions; api/settings.py:495,502,512 and web queries.ts:189,192, mutations.ts:636 call them. | high |
| `calibration_items` | keep | 0; answered_at max NULL, no insertion timestamp (Q3). feedback/calibrate.py:107 (_add), more and answer write; session/sessions read; api/settings.py:520,528 and mutations.ts:664,673 are actual UI callers. | high |
| `owner` replay cohort | keep | 0 frozen production labels (Q2); **217** maintained CSV rows in evals/labels/fit.csv, counted with csv.DictReader. quality/replay.py:71–80 loads them; :98 selects; :309 freezes. Counts match evals/labels/README.md:19. CSV provisional/legacy labels are not old live LabelOrigin states. | high |
| `provisional` replay cohort | keep | 0 frozen production labels (Q2); **86** maintained CSV rows in evals/labels/fit.csv, counted with csv.DictReader. quality/replay.py:71–80 loads them; :98 selects; :309 freezes. Counts match evals/labels/README.md:19. CSV provisional/legacy labels are not old live LabelOrigin states. | high |
| `legacy` replay cohort | keep | 0 frozen production labels (Q2); **953** maintained CSV rows in evals/labels/regression.csv, counted with csv.DictReader. quality/replay.py:71–80 loads them; :98 selects; :309 freezes. Counts match evals/labels/README.md:19. CSV provisional/legacy labels are not old live LabelOrigin states. | high |
| `silver` replay cohort | keep | 0 frozen production labels (Q2); **200** maintained CSV rows in evals/labels/regression.csv, counted with csv.DictReader. quality/replay.py:71–80 loads them; :98 selects; :309 freezes. Counts match evals/labels/README.md:19. CSV provisional/legacy labels are not old live LabelOrigin states. | high |
| `miss` replay cohort | keep | 0 frozen production labels and 0 misses; last reported_at NULL (Q8). feedback/misses.py:report writes misses; quality/replay.py:85 loads them for replay. api/reading.py:922 + web mutations.ts:158 is a reachable producer. | high |

Other classifier inputs remain attached to the classifier harness pending Q9: `tiers-0.14.csv` 23,310 rows, `silver-sample.csv` 200, `silver.csv` 165, `disputed.csv` 35 and `adjudicated.csv` 20. `quality/classify.py:load/report` reads these, and `docs/agents/verification.md:22` still prescribes that harness. Their historical names do not make them dead cohorts. All counts above are file rows, not assertions that every paper is currently available in production. Preserve the dated reports and their inputs as evidence even if the owner retires a command. A4's “Evals as checks” and “After the fold: N9” own that separation (`docs/backlog.md`).

## API routes

Inventory covers every method decorator in `src/litwatch/api/*.py`, the `/assets` and `/mcp` mounts, and FastAPI's implicit `/openapi.json`. Query parameters do not make new routes. `{paper_id}` below preserves the server's `:path` converter. Keep means a runtime caller or an operational/development contract exists; it does **not** claim observed traffic. There is no access log. The five explicit endpoints with no runtime repo caller, plus the implicit OpenAPI endpoint, are owner-to-confirm, including the documented polling and install surfaces.

Search method: source AST enumerated decorators and router prefixes (`protected(...)` / `APIRouter(...)`), then method-specific `rg -n 'api\.(GET|POST|PUT|DELETE)\(' web/src --glob '!*.test.*' --glob '!schema.d.ts'`. Every JSON `/api` route also has a declaration in generated `web/src/api/schema.d.ts`; those declarations were checked with `rg -nF '<path>' web/src/api/schema.d.ts` and are **not runtime callers**. `rg -n '"/api/|`/api/|reading\.|integrations_api\.|bibtex|lookup\(' src/litwatch/api/mcp.py src/litwatch/cli.py deep/src/hosts.ts` plus direct-function tracing found:

- MCP calls store readers and shared helpers (`api/mcp.py:325,331,368`), **no HTTP route**; its `_bibtex` helper must remain even if a BibTeX route goes.
- CLI commands call Python services/stores, **no HTTP route**. `openapi` calls `app.openapi()` directly, not `/openapi.json`.
- Deep's sole litwatch HTTP caller at this boundary is `GET /api/papers` (`deep/src/hosts.ts:171`). No Deep internals were audited.
- Tests were traced by method and literal/f-string path in `tests/` and through web test mocks. A representative direct test is given where found. Generated schemas are not tests; a test is not evidence of production traffic.

In the table, source locations are under `src/litwatch/`; web locations under `web/src/`. “Schema” means the generated declaration exists. The two Deep-specific API rows record their web callers only; production's older deployed revision is not used to judge those new routes.

| Item | Decision | Evidence | Confidence |
| --- | --- | --- | --- |
| `GET /healthz` | keep | `api/app.py:152`. Container healthcheck in deploy/compose.yaml; tests/test_api.py:55. Operational liveness contract. | high |
| `GET /{path}` | keep | `api/app.py:174`. Browser navigation and share targets; app.py:_serve_web serves built index/static files. Framework catch-all, not a JSON API. | high |
| `GET /` | keep | `api/app.py:165`. Conditional unbuilt-app page when package has no index.html; tests/test_api.py:91. Development fallback, not production traffic. | high |
| `GET /api/digests/days/{day}` | keep | `api/digests.py:122`. Web: api/queries.ts:139. Schema. Test: tests/test_digests.py:1499. | high |
| `GET /api/digests/weeks/{day}` | keep | `api/digests.py:148`. Web: api/queries.ts:142. Schema. Test: tests/test_digests.py:1512. | high |
| `GET /api/digests` | keep | `api/digests.py:161`. Web: api/queries.ts:151. Schema. Test: tests/test_digests.py:1519. | high |
| `GET /api/digests/{digest_id}` | keep | `api/digests.py:217`. Web: api/queries.ts:144, api/mutations.ts:450. Schema. Test: tests/test_digests.py:1526. | high |
| `POST /api/digests/{digest_id}/write` | keep | `api/digests.py:224`. Web: api/mutations.ts:439. Schema. Test: tests/test_digests.py:1649. | high |
| `GET /api/settings/integrations` | keep | `api/integrations.py:67`. Web: api/queries.ts:220. Schema. Test: tests/test_api_integrations.py:114. | high |
| `POST /api/settings/integrations/{name}/retry` | keep | `api/integrations.py:135`. Web: api/mutations.ts:687. Schema. Test: tests/test_api_integrations.py:131. | high |
| `POST /api/settings/integrations/{name}/skip` | keep | `api/integrations.py:144`. Web: api/mutations.ts:706. Schema. Test: tests/test_api_integrations.py:134. | high |
| `GET /api/events` | owner to confirm | `api/integrations.py:169`. Q5; documented external polling contract, docs/integrations.md:362; tests/test_api_integrations.py:245. Schema. Test: tests/test_api_integrations.py:245. | medium |
| `POST /api/papers/{paper_id}/card` | keep | `api/integrations.py:197`. Web: api/mutations.ts:270. Deep boundary only, excluded implementation. Schema. Test: tests/test_api_integrations.py:155. | high |
| `GET /api/search/scope` | keep | `api/integrations.py:310`. Web: api/queries.ts:41. Schema. Test: tests/test_api_integrations.py:235. | high |
| `GET /api/jobs` | keep | `api/jobs.py:122`. Web: api/queries.ts:199. Schema. Test: tests/test_digests.py:1615. | high |
| `POST /api/jobs/process` | keep | `api/jobs.py:138`. Web: api/mutations.ts:169. Schema. Test: tests/test_jobs.py:641. | high |
| `GET /api/jobs/reruns/offer` | keep | `api/jobs.py:252`. Web: api/queries.ts:205. Schema. Test: tests/test_reruns.py:748. | high |
| `POST /api/jobs/reruns` | keep | `api/jobs.py:267`. Web: api/mutations.ts:215. Schema. Test: tests/test_reruns.py:773. | high |
| `GET /api/jobs/replays/offer` | keep | `api/jobs.py:353`. Web: api/queries.ts:213. Schema. Test: tests/test_replay.py:458. | high |
| `POST /api/jobs/replays` | keep | `api/jobs.py:363`. Web: api/mutations.ts:505. Schema. Test: tests/test_replay.py:448. | high |
| `POST /api/jobs/{job_id}/{action}` | keep | `api/jobs.py:401`. Web: api/mutations.ts:188, api/mutations.ts:238. Schema. Test: tests/test_jobs.py:607. | high |
| `GET /metrics` | keep | `api/monitor.py:543`. Prometheus on dedicated port 9110, stack README Operations and telemetry configuration; tests/test_mcp.py:702. | high |
| `GET /status` | keep | `api/monitor.py:547`. Vergil/stack operational status on 9110, distinct from /api/status; stack README Operations; tests/test_mcp.py:683. | high |
| `GET /api/status` | owner to confirm | `api/reading.py:198`. Q4; docs/install.md:220 documents a manual check; web now uses summary. tests/test_reading.py:526 and many fixtures remain; run_status is also used by summary and must stay. Schema. Test: tests/test_acknowledge.py:125. | medium |
| `GET /api/papers` | keep | `api/reading.py:224`. Web: api/queries.ts:96, api/queries.ts:110. Deep: deep/src/hosts.ts:171; MCP shares the store reader. Schema. Test: tests/test_api.py:35. | high |
| `GET /api/papers/{paper_id}/deep/figures/{name}` | keep | `api/reading.py:262`. Web template in components/paper/PaperRead.tsx:28; newer Deep boundary only, excluded implementation. Schema. Test: tests/test_deep.py:694. | high |
| `GET /api/papers/{paper_id}/pdf` | keep | `api/reading.py:298`. Returned pdf_url → components/paper/Actions.tsx:93 and Ladder.tsx:64 (browser href); reading.py:298 builds the response. No literal api.GET call needed. Schema. Test: tests/test_deep.py:730. | high |
| `GET /api/papers/{paper_id}` | keep | `api/reading.py:334`. Web: api/queries.ts:35. Schema. Test: tests/test_api.py:37. | high |
| `PUT /api/papers/{paper_id}/publication` | keep | `api/reading.py:641`. Web: api/mutations.ts:294. Schema. Test: tests/test_publications.py:235. | high |
| `DELETE /api/papers/{paper_id}/publication` | owner to confirm | `api/reading.py:671`. Q2; no runtime caller; tests/test_publications.py:245. PUT none=true is a different action: DELETE forgets override, so do not equate them. Schema. Test: tests/test_publications.py:245. | medium |
| `POST /api/bibtex` | keep | `api/reading.py:716`. Web: api/mutations.ts:556. Schema. Test: tests/test_publications.py:226. | high |
| `GET /api/bibtex` | owner to confirm | `api/reading.py:742`. Q1; no runtime caller; tests/test_reading.py:865,868. POST is called at mutations.ts:556. Preserve shared _bibtex (also MCP) and _matching (also POST all/filter). Schema. Test: tests/test_reading.py:865. | medium |
| `POST /api/lookup` | owner to confirm | `api/reading.py:748`. Q3; no runtime caller; tests/test_reading.py:936. Preserve feedback/misses.lookup and fetch_by_ids, which other services use. Schema. Test: tests/test_reading.py:936. | medium |
| `GET /api/topics` | keep | `api/reading.py:787`. Web: api/queries.ts:28. Schema. Test: tests/test_digests.py:1528. | high |
| `GET /api/topics/{slug}` | keep | `api/reading.py:813`. Web: api/queries.ts:121. Schema. Test: tests/test_reading.py:328. | high |
| `PUT /api/reactions/{paper_id}` | keep | `api/reading.py:858`. Web: api/mutations.ts:121. Schema. Test: tests/test_mcp.py:262. | high |
| `DELETE /api/reactions/{paper_id}` | keep | `api/reading.py:880`. Web: api/mutations.ts:130. Schema. Test: tests/test_reading.py:455. | high |
| `PUT /api/library/{paper_id}` | keep | `api/reading.py:895`. Web: api/mutations.ts:112. Schema. Test: tests/test_mcp.py:606. | high |
| `PUT /api/labels/{topic}/{paper_id}` | keep | `api/reading.py:904`. Web: api/mutations.ts:141. Schema. Test: tests/test_reading.py:261. | high |
| `DELETE /api/labels/{topic}/{paper_id}` | keep | `api/reading.py:916`. Web: api/mutations.ts:150. Schema. Test: tests/test_reading.py:747. | high |
| `POST /api/misses` | keep | `api/reading.py:922`. Web: api/mutations.ts:158. Schema. Test: tests/test_reading.py:577. | high |
| `POST /api/session` | keep | `api/reading.py:952`. Web: api/client.ts:46. Schema. Test: tests/test_api.py:49. | high |
| `GET /api/settings/taxonomy` | keep | `api/settings.py:42`. Web: api/queries.ts:48, api/queries.ts:224. Schema. Test: tests/test_settings.py:57. | high |
| `GET /api/settings/taxonomy.yaml` | keep | `api/settings.py:48`. Web download anchor routes/settings/taxonomy.tsx:58. Schema. Test: tests/test_settings.py:313. | high |
| `GET /api/settings/taxonomy/stats` | keep | `api/settings.py:76`. Web: api/queries.ts:226. Schema. Test: tests/test_settings.py:303. | high |
| `GET /api/settings/topics/{slug}/keywords` | keep | `api/settings.py:86`. Web: api/queries.ts:230. Schema. Test: tests/test_settings.py:310. | high |
| `PUT /api/settings/topics/{slug}` | keep | `api/settings.py:96`. Web: api/mutations.ts:575. Schema. Test: tests/test_settings.py:60. | high |
| `PUT /api/settings/topics-order` | keep | `api/settings.py:121`. Web: api/mutations.ts:613. Schema. Test: tests/test_settings.py:90. | high |
| `GET /api/settings/quality` | keep | `api/settings.py:134`. Web: api/queries.ts:185. Schema. Test: tests/test_acknowledge.py:102. | high |
| `POST /api/settings/acknowledge` | keep | `api/settings.py:209`. Web: api/mutations.ts:520. Schema. Test: tests/test_acknowledge.py:116. | high |
| `GET /api/settings/misses` | keep | `api/settings.py:275`. Web: api/queries.ts:187. Schema. Test: tests/test_settings.py:221. | high |
| `POST /api/settings/classification/{paper_id}/dismiss` | keep | `api/settings.py:354`. Web: api/mutations.ts:339. Schema. Test: tests/test_steps.py:533. | high |
| `PUT /api/settings/classification/{paper_id}` | keep | `api/settings.py:366`. Web: api/mutations.ts:348. Schema. Test: tests/test_steps.py:545. | high |
| `POST /api/settings/classification/{paper_id}/correction` | keep | `api/settings.py:390`. Web: api/mutations.ts:357. Schema. Test: tests/test_steps.py:569. | high |
| `POST /api/settings/analysis/{paper_id}/retry` | keep | `api/settings.py:403`. Web: api/mutations.ts:366. Schema. Test: tests/test_acknowledge.py:198. | high |
| `POST /api/settings/grading/{topic}/{paper_id}/now` | keep | `api/settings.py:418`. Web: api/mutations.ts:379. Schema. Test via formatted URL: tests/test_grading.py:258,266. | high |
| `POST /api/settings/fulltext/{pass_id}/now` | keep | `api/settings.py:461`. Web: api/mutations.ts:239, api/mutations.ts:404. Schema. Test: tests/test_acknowledge.py:201. | high |
| `POST /api/settings/fulltext/{pass_id}/pdf` | keep | `api/settings.py:470`. Web: api/mutations.ts:404. Schema. Test: tests/test_fulltext.py:508. | high |
| `POST /api/settings/calibrate` | keep | `api/settings.py:495`. Web: api/mutations.ts:636. Schema. Tests searched; no direct literal call found. | high |
| `GET /api/settings/calibrate` | keep | `api/settings.py:502`. Web: api/queries.ts:189. Schema. Test: tests/test_calibrate.py:310. | high |
| `GET /api/settings/calibrate/{session_id}` | keep | `api/settings.py:512`. Web: api/queries.ts:192. Schema. Web test: web/src/routes/settings.test.tsx:357. | high |
| `POST /api/settings/calibrate/{session_id}/more` | keep | `api/settings.py:520`. Web: api/mutations.ts:673. Schema. Test: tests/test_settings.py:194. | high |
| `PUT /api/settings/calibrate/{session_id}/items/{position}` | keep | `api/settings.py:528`. Web: api/mutations.ts:664. Schema. Web test: web/src/routes/settings.test.tsx:859. | high |
| `GET /api/settings/runs` | keep | `api/settings.py:541`. Web: api/queries.ts:194. Schema. Test: tests/test_jobs.py:103. | high |
| `GET /api/settings/schedule` | keep | `api/settings.py:561`. Web: api/queries.ts:196. Schema. Test: tests/test_settings.py:107. | high |
| `GET /api/settings/config` | keep | `api/settings.py:576`. Web: api/queries.ts:217. Schema. Test: tests/test_settings.py:147. | high |
| `GET /api/summary` | keep | `api/summary.py:20`. Web: api/queries.ts:15. Schema. Test: tests/test_acknowledge.py:133. | high |
| `/assets/*` mount | keep | api/app.py:172; built index.html loads Vite assets; same-origin browser runtime. Not an OpenAPI route. | high |
| `/mcp` SDK route | keep | api/mcp.py:177–192; configured allowed host, stack README research-agent connection; SDK transport and its tools are the callers. Auth/environment values were not inspected. | high |
| `GET /openapi.json` implicit | owner to confirm | Q6; api/app.py:127 creates FastAPI with its default openapi_url; docs/redoc are disabled, openapi_url is not. No repo HTTP caller; CLI openapi uses app.openapi() at cli.py:910. No traffic log. | medium |

## CLI commands

All 25 leaf commands/callbacks in `cli.py` were enumerated from Typer decorators and `add_typer` names, without invoking the CLI. The group-only help callbacks are not extra commands. Deep trace/notes are recorded only as existing documented read-only boundaries, not investigated internally.

Caller search: `rg -n 'litwatch <command>\b' README.md AGENTS.md docs scripts deploy .github the stack README` per command, with multi-line `eval ladders-report` text followed manually. Also searched command arrays and `ExecStart` in compose, repo scripts, tracked Dell systemd/backup definitions, `/etc/cron.d`, `/etc/crontab` and user systemd files, excluding `.env*`, key files and names containing waits. Compose starts **serve**; the nightly/publications timers are in-process Python scheduler calls, not recurring CLI invocations. No litwatch CLI match was found in readable cron definitions. User `crontab -l` was unavailable (exit 1); `/var/spool/cron/crontabs` was unreadable. Private root crontabs, arbitrary home scripts and shell history were not audited. No claim of complete external caller coverage is made.

Q3's run kinds provide service-use evidence, not attribution to a particular command: a scheduler/API invocation can leave the same kind. Manual-only commands therefore remain owner-to-confirm even with recent underlying work. Read-only commands such as export/check/report have no execution ledger. File traces below have **report dates**, not a fabricated execution timestamp; checkout mtimes were checked but are not reliable run times. Classifier/Brief/fallback reports are from before or outside this production database.

| Item | Decision | Evidence | Confidence |
| --- | --- | --- | --- |
| `litwatch db migrate` | keep | `src/litwatch/cli.py:85`. README.md:35; scripts/refresh-sandbox.sh:103 and install.md:139 execute it. serve also applies migrations. Q4: migration 41 applied 2026-11-09 20:51 UTC (not proof the CLI performed it). | high |
| `litwatch intake` | owner to confirm | Q7; `src/litwatch/cli.py:92`. README.md:49; archive-catalog-pass plan also uses --catalog-from. Q3: 2 intake runs, latest 2026-10-31 10:59 UTC; scheduler nightly also performs intake. | medium |
| `litwatch intake archive` | keep | `src/litwatch/cli.py:147`. README.md:50; scripts/refresh-sandbox.sh:122 is a real development/sandbox caller. Production has 298 archive-sourced papers (Q4); Q3 intake last 2026-10-31 10:59 UTC does not distinguish archive vs OAI command. | high |
| `litwatch nightly` | owner to confirm | Q7; `src/litwatch/cli.py:185`. README.md:46; stack README:121 has manual invocation. Q3: 25 nightly runs, latest 2026-11-10 09:00 UTC, finished 09:44; scheduler calls Python directly. | medium |
| `litwatch digests write` | owner to confirm | Q7; `src/litwatch/cli.py:217`. README.md:51; stack switch recovery docs. Q3: 2 digests-kind runs, latest 2026-10-23 17:01 UTC; 141 digests, latest write 2026-11-10 09:44 from any producer. | medium |
| `litwatch deep trace` | keep | `src/litwatch/cli.py:234`. docs/agents/dev-host.md documents docker exec litwatch litwatch deep trace as read-only; Deep internals excluded, no invocation history queried. | high |
| `litwatch deep notes` | keep | `src/litwatch/cli.py:281`. docs/agents/dev-host.md documents docker exec litwatch litwatch deep notes as read-only; Deep internals excluded, no invocation history queried. | high |
| `litwatch taxonomy import` | keep | `src/litwatch/cli.py:311`. deploy/README.md:15 and docs/install.md:157,166 require it; only CLI imports YAML today (backlog A3). Q3 classes last changed 2026-10-17 03:55 UTC; no import execution ledger. | high |
| `litwatch taxonomy export` | owner to confirm | Q8; `src/litwatch/cli.py:330`. docs/install.md:164; same YAML is downloadable in the web. Read-only; no command-specific trace. | medium |
| `litwatch taxonomy check` | owner to confirm | Q8; `src/litwatch/cli.py:345`. README.md:57; docs/install.md:165. Local file validation; no production trace by design. | medium |
| `litwatch eval classify` | owner to confirm | Q9; `src/litwatch/cli.py:364`. README.md:52, verification.md:22. evals/2026-10-14-classifiers.md:3 reports 2026-10-14 run; jev/haiku/luna-decisions.jsonl.gz survive. Writes JSONL, no eval DB tables or runs record. | medium |
| `litwatch eval classify-report` | owner to confirm | Q9; `src/litwatch/cli.py:411`. verification.md:22; classifier report:8 names exact command. Same 2026-10-14 result files; read-only, report regeneration has no execution timestamp. | medium |
| `litwatch eval replay` | owner to confirm | Q12; `src/litwatch/cli.py:435`. README.md:53; web Run replay is another caller of the same Python services. Q2: no eval runs/results/labels; Q3: no evaluation run kind. evals/2026-10-15-replay.md is historical evidence, not a row in this DB. | medium |
| `litwatch eval report` | owner to confirm | Q12; `src/litwatch/cli.py:512`. README.md:55; reads eval_runs/results/labels, all empty in production (Q2). No report-execution trace; historical replay report survives in evals/. | medium |
| `litwatch eval digests` | owner to confirm | Q13; `src/litwatch/cli.py:540`. milestones.md names the voice check; cli.py:540 calls quality/digests.py over written digests. 141 digests available (Q3); command prints a report and leaves no run record. | medium |
| `litwatch eval briefs` | owner to confirm | Q10; `src/litwatch/cli.py:573`. verification.md:134; evals/2026-10-16-sonnet-briefs.md:11 records actual command variants with JSONL results. Last identified maintained comparison dated 2026-10-16; no production journal record expected (gateway built without DB journal). | medium |
| `litwatch eval briefs-report` | owner to confirm | Q10; `src/litwatch/cli.py:629`. verification.md:134; consumes Brief preference YAML, JSONL and DB abstracts/gists (cli.py:629). Surviving 2026-10-16 Sonnet report/results; no report invocation ledger. | medium |
| `litwatch eval ladders` | owner to confirm | Q11; `src/litwatch/cli.py:658`. failure-handling plan/P1 and evals/2026-10-19-fallback-ladders.md:3 describe a 2026-10-19 run on fh_p1_sandbox. JSONL case/model outputs survive; costs live in file ledger, not production eval tables. | medium |
| `litwatch eval ladders-report` | owner to confirm | Q11; `src/litwatch/cli.py:753`. evals/2026-10-19-fallback-ladders.md:19–20 names this command; tables.md survives. Current-code request reconstruction is intentional; no command execution ledger. | medium |
| `litwatch library sync` | owner to confirm | Q7; `src/litwatch/cli.py:796`. README.md:59; cli.py:796 is the manual settle/reconciliation recovery. No run record for this command; nightly settlement uses the same rules and must remain. | medium |
| `litwatch publications` | owner to confirm | Q7; `src/litwatch/cli.py:803`. README.md:60; same lookup as scheduler. Q3: 25 publications runs, latest 2026-11-10 16:30 UTC; 234 publication rows, latest 16:30:58. Not attributable to CLI. | medium |
| `litwatch serve` | keep | `src/litwatch/cli.py:822`. deploy/compose.yaml:40 and live stack docker-compose.yml:55 start it. Deployed image verified fe1cefd; recent nightly/publication work in Q3 is from this service or equivalent manual calls. | high |
| `litwatch openapi` | keep | `src/litwatch/cli.py:903`. AGENTS.md:48 and docs/agents/verification.md:35 require it after API changes; generates web/openapi.json (not a DB write). Generated client is used by the web; no execution ledger. | high |
| `litwatch fulltext` | owner to confirm | Q7; `src/litwatch/cli.py:915`. docs/specs/v1/pipeline.md:234 and switch recovery docs. Q3: 36 fulltext runs, latest 2026-11-07 14:43 UTC; cannot separate worker vs CLI. --deep implementation excluded. | medium |
| `litwatch ping` | owner to confirm | Q14; `src/litwatch/cli.py:981`. README.md:40, install.md:228–229, verification.md:30. cli.py:981 builds a gateway without journal; no production call/run trace by design. It makes a paid request, so it was not executed. | medium |

## Migration-era guards and compatibility paths

This distinguishes obsolete shapes with **no current producer** from legitimate states a future paper, retry or owner action can create. Q4/Q6/Q7 supply the row evidence. `rg -n '0\.x|0\.14|compatib|legacy|provisional' src/litwatch --glob '*.py'` (excluding the Deep areas), followed by all constructor/store callers, was the starting search. No zero-row observation alone is used to delete a reachable future behavior.

| Item | Decision | Evidence | Confidence |
| --- | --- | --- | --- |
| Old `eval_results.triage_yes`, `triage_why`, `grade` columns | drop | Q2/Q4: 0 results and 0 populated old columns. Only old migration defines triage fields; current writer stores classifier/fit (store/evaluation.py:105,144), readers use those (core/evaluation.py:28, store/evaluation.py:182). Migration 0021 explicitly left old columns as history; this DB has none. D1. | high |
| Redundant `eval_runs.run_id` first-run field | drop | Q2/Q4: 0 eval runs; no old record needs a single-run bridge. evaluation.py:56 still writes both run_id and run_ids; :36 costs only ANY(e.run_ids); replay resume/report and Overview never read singular run_id. Search: rg -n run_id src/litwatch/{store/evaluation.py,quality/replay.py,jobs/replays.py,store/quality.py}. D1 removes the redundant write with the column. | high |
| LabelOrigin `candidate_sample`, `reject_sample`, `legacy_positive`, `gold` | drop | Q6: 0 labels of those origins; only current label is reader/related, nonprovisional. No current source constructor sets those four: only enum definitions core/types.py:97–100. Active producers use sample, reader or miss (calibrate.py:238, api/reading.py:908, misses.py:80). Historical CSV cohort/source strings stay. D2. | high |
| Live-label `provisional` migration handling | drop | Q6: 0 provisional labels. Current producers omit it and get false; no bulk-accept/import command exists. core/rules.py:50, store/feedback.py:29 and predicates in feedback/calibrate.py, store/reading.py, api/reading.py handle imported bulk labels only; tests/test_grading_rules.py:154 is the true-valued constructor. This is distinct from the active CSV provisional cohort. D3; proposal limited to current installation, owner approval required for other installs/history. | medium |
| Missing/invalid database paper revision → 1 fallback | drop | Q4: 19,169 papers, 0 revision NULL/<1; Q6 schema has NOT NULL DEFAULT 1. papers.py:47 uses row.get("revision") or 1. All callers in papers/classifications/analyses/fulltext use COLUMNS including p.revision (papers.py:18); no caller passes old partial dicts. Replace with row["revision"] as A1 rewrites repositories. D4. | high |
| Wrong-database 0.x refusal | keep | Q4: selections is absent and schema_migrations exists. That is the healthy outcome, not evidence the guard is dead: store/db.py:70–76 must reject an accidental old-DB target before creating any migration table. Test: tests/test_store.py (LegacyDatabaseError). Future install/operator error is reachable. | high |
| Applied migration chain, including teardown migrations | keep | Q4: 41 migrations applied; triage and set_asides absent because 0016/0024 dropped them. A fresh installation still executes the complete chain. Keep existing migration files unchanged and add forward cleanup migrations. | high |
| Unknown version, announcement and revision-history handling | keep | Q4: no unknown current arXiv version, but 1 paper has announced_known=false; Q7: 0 non-array histories. External intake may lack historical/version data again (core/Paper permits None); absence today does not make those guards obsolete. Keep core/digests and store/digests handling; archive implementation excluded. | high |
| Old-text standing and abandoned-work recovery | keep | Q7: 218 papers have revision>1, 7,625 have arXiv version>1, and one step_failure exists. core.current_verdict and nightly reconciliation/take_back protect live rewrites and crashes. Source: core/grading.py, jobs/nightly.py:111, fulltext/store retry code. Not migration leftovers. | high |
| Zotero 0.x tag/note compatibility while integration is retained | keep | zotero_items=0 and cursor=0 (Q3) say nothing about the owner's external Zotero library. LEGACY_PREFIXES/NOTE_FIND at integrations/zotero.py:57,67 deliberately reconcile old items when enabled; stack README P4 describes that adoption. No external-library query was authorized/performed. Keep this path unless Q15 retires the whole integration. | medium |

## Other integration paths

`rg -n 'BUILT_IN|def load|enabled_using|options\.(get|pop)' src/litwatch/integrations` and each loader's caller were traced. The loader accepts explicitly configured built-ins or importable modules; lack of an instance in this one file does not prove the extension API unused everywhere. Credentials and external account contents were deliberately not checked.

| Item | Decision | Evidence | Confidence |
| --- | --- | --- | --- |
| Notion export | keep | Non-secret TOML enables notion with all required non-secret options. Q3: 263 notion_pages, latest 2026-11-10 09:49:21 UTC; cursor 104966 updated 09:50:26. integrations/notion.py:1059 loads it; active outbox consumer. | high |
| Zotero grade-sync | owner to confirm | Q15. No production section; 0 zotero_items, cursor position 0 unchanged since 2026-10-17 03:55:56 UTC. Code integrates only if configured (integrations/__init__.py:61); tests/test_zotero.py exercises it, not production. Backlog A5 already moves its client; “Outside the rework: Save to Zotero” proposes a replacement but needs shaping. | medium |
| WebDAV PDF sync | owner to confirm | Q16. No production section; 0 webdav_files, cursor 0 since 2026-10-17 03:55:56 UTC. integrations/webdav.py:load requires Zotero. No evidence of current execution, but stack README deliberately preserves enabling it later; external share not inspected. | medium |
| Generic webhook | owner to confirm | Q17. No production section or cursor; configured integrations are archive/deep/notion. integrations/webhook.py:load remains an explicit supported extension in docs/integrations.md:229,342; tests/test_integrations.py cover it. No runtime caller when unconfigured. A5 rewrites HTTP clients. | medium |

Archive integration is demonstrably configured (`harvest=true`, cursor 104966) but its removal/fold belongs to A6 and was excluded. Deep remains configured; removing its redundant dispatcher registration is already an A0 item and was excluded. No additional unreferenced module was established in this pass; “unconfigured integration” is not being presented as dead Python imports.

## What each proposed drop touches

There are seven drop rows: two tables and five compatibility/schema items. They consolidate into four removal scopes below. Existing migration files stay; an additive migration performs any actual schema removal. These are recommendations only; P3 still needs the owner's keep/drop marks and its fresh-schema/copied-production verification. No migration was run for this analysis.

- **D1 — unused evaluation storage:** drop `regression`, `eval_samples`, `eval_results.{triage_yes,triage_why,grade}` and `eval_runs.run_id`; remove the singular-field assignment from `store/evaluation.py:61`. Keep `eval_runs.run_ids`, `paper_version`, current classifier/fit/opinion/error fields, the three active evaluation tables, and every labelled CSV. `SELECT e.*` / `r.*` exports shrink, so check replay/report snapshots and unknown owner SQL consumers before applying. Touches: one new `store/migrations/` file, `store/evaluation.py`, schema expectations in `tests/test_store.py` / replay tests as needed, data/spec evaluation-table documentation. No config keys, roles or web controls removed. **A0 owns these obsolete structures** (`docs/backlog.md`, A0 “What production still uses”); A1 will consume the repositories and A4's “Evals as checks” restructures harnesses, but neither backlog item already promises an evaluation-schema rewrite. Do not use A4 to postpone the independent dead-table removal.
- **D2 — unused live-label origins:** remove just the four `LabelOrigin` enum members in `core/types.py`; update historical-live-label prose in `docs/specs/v1/data.md` / feedback docs if they describe those as supported values. `origin` is unconstrained text in SQL, and the API's `LabelState.origin` is a string, so no database migration or generated type change is intrinsically required; search typed fixtures and imports. No config key or cohort removal. **A0**, before A1 migrates label repositories; no later backlog item explicitly rewrites origin meanings. Historical reports/CSV origins and fixtures for replay file parsing must remain readable.
- **D3 — provisional live-label support:** remove the live `Label.provisional` field, `labels.provisional` column in a new migration, its bulk-label precedence branch in `core/rules.py`, the origin-replacement CASE in `store/feedback.py`, associated Calibrate/shelf/Needs-you predicates, and `api/schemas.LabelState.provisional`. Touches `core/types.py`, `core/rules.py`, `store/{feedback,reading}.py`, `feedback/calibrate.py`, `api/{reading,schemas}.py`, API schemas/generated client, `tests/test_grading_rules.py` and API label fixtures (`tests/test_reading.py:508,586`); check web type uses. **No** removal of the `provisional` replay cohort or its 86 CSV labels. No config keys. **Leave this coordinated simplification to A1**, whose settling/read rewrite already touches those paths (`docs/backlog.md`, A1 “Settling a paper made cheap”, “Calibrate reads each topic's pool once”, “SQL outside store”). Preserve it until that stage so A0 does not rewrite the same settlement surface first.
- **D4 — obsolete row revision default:** simplify `store/papers.py:47` to use the required revision column; check its `COLUMNS` consumers and store tests. No migration, config key or API field change. **Leave to A1**, which rewrites repositories and their database contracts (`docs/backlog.md`, A1 Database gate).

The largest conditional removals are Zotero/WebDAV and unused terminal harnesses, but they remain owner-to-confirm, not drops. If Q15–Q17 retire integrations, scope must include their registry entries, modules, tests, integration docs and any obsolete cursor/mapping tables; preserve external Zotero/share data. A5 already rewrites outgoing HTTP and the Zotero client, while Save to Zotero is still unshaped. If terminal evaluation harnesses retire, preserve their dated inputs/results and any shared validators/request builders used by live services. CLI scheduling/submission is already rewritten in **A2**; harness/input separation is already **A4**. Those are later-stage scope decisions, not permission to erase evidence now.

## Query evidence

These are the decisive queries and their measured results. Every script used this prefix as separate psql statements, and ended with COMMIT:

```sql
SET default_transaction_read_only = on;
SET statement_timeout = '20s';
BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;
SELECT current_timestamp, current_setting('transaction_read_only'); -- on
```

**Q1 — roles:**

```sql
SELECT operation, count(*) AS all_time,
       count(*) FILTER (WHERE started_at >= now() - interval '30 days') AS days30,
       min(started_at) AS first_at, max(started_at) AS last_at
FROM inference_calls GROUP BY operation ORDER BY operation;
```

Eight rows, exactly the counts in the role table; first call 2026-10-17 08:01:59 UTC (classify). No observed operation falls outside config.ROLES.

**Q2 — evaluation:**

```sql
SELECT count(*), max(started_at), max(finished_at) FROM eval_runs;
SELECT kind, count(*), max(started_at), max(finished_at)
FROM eval_runs GROUP BY kind;
SELECT 'eval_results' AS name, count(*) FROM eval_results
UNION ALL SELECT 'eval_labels', count(*) FROM eval_labels
UNION ALL SELECT 'eval_samples', count(*) FROM eval_samples
UNION ALL SELECT 'regression', count(*) FROM regression;
SELECT l.cohort, count(*), max(e.started_at), max(e.finished_at)
FROM eval_labels l JOIN eval_runs e ON e.id=l.eval_run_id GROUP BY l.cohort;
```

Runs 0, timestamps NULL; every child/legacy count 0; kind and cohort grouping empty. No surviving last write can be claimed.

**Q3 — command/service and integration traces:**

```sql
SELECT kind, count(*), max(started_at), max(finished_at)
FROM runs GROUP BY kind ORDER BY kind;
SELECT kind, state, count(*), max(created_at), max(changed_at)
FROM jobs GROUP BY kind, state;
SELECT name, position, failures, setup_failures, updated_at
FROM integration_cursors ORDER BY name;
-- For each table below:
SELECT count(*), max(written_at) FROM digests;
SELECT count(*), max(checked_at) FROM publications;
SELECT count(*), max(updated_at) FROM classes;
SELECT count(*), max(drawn_at) FROM calibration_sessions;
SELECT count(*), max(answered_at) FROM calibration_items;
SELECT count(*), max(updated_at) FROM zotero_items;
SELECT count(*), max(updated_at) FROM webdav_files;
SELECT count(*), max(updated_at) FROM notion_pages;
```

Run kinds: digests 2 (latest 2026-10-23 17:01 UTC); fulltext 36 (11-07 14:43); grading 2 (10-20 16:18); intake 2 (10-31 10:59); job 6 (11-04 15:58); nightly 25 (11-10 09:00); publications 25 (11-10 16:30). Jobs: only digest/done, 6, last changed 2026-11-04 15:58:08 UTC. No replay or rerun jobs.

Digests 141, publications 234, classes 58, calibration sessions/items 0/0, Zotero items 0, WebDAV files 0, Notion pages 263. Latest times are reported in their area tables. Cursors archive/deep/notion each 104966, all updated on 11-10 around 09:50 UTC; Zotero/WebDAV each 0 since 10-17. All five have failures=0, setup_failures=0; zero failures is not evidence a disabled integration ever ran.

**Q4 — migration shapes and paper state:**

```sql
SELECT count(*) AS papers,
 count(*) FILTER (WHERE revision IS NULL OR revision<1) AS invalid_revision,
 count(*) FILTER (WHERE version IS NULL) AS unknown_version,
 count(*) FILTER (WHERE announced_known=false) AS unknown_announced
FROM papers;
SELECT metadata_source, count(*) FROM papers GROUP BY metadata_source;
SELECT count(*) AS results,
 count(*) FILTER (WHERE triage_yes IS NOT NULL) AS triage_yes,
 count(*) FILTER (WHERE triage_why IS NOT NULL) AS triage_why,
 count(*) FILTER (WHERE grade IS NOT NULL) AS grade FROM eval_results;
SELECT count(*), count(*) FILTER (WHERE run_id IS NOT NULL),
 count(*) FILTER (WHERE run_id IS NOT NULL AND NOT run_id=ANY(run_ids)) FROM eval_runs;
SELECT to_regclass('public.selections'), to_regclass('public.triage'),
       to_regclass('public.set_asides');
SELECT count(*), max(version), max(applied_at) FROM schema_migrations;
```

Papers **19169 / 0 / 0 / 1**; metadata sources oai 18870, archive 298, arxiv 1. Eval-result counts all 0; eval-run counts all 0; old relations all NULL. Migrations **41 / 41 / 2026-11-09 20:51:16 UTC**.

**Q6 — obsolete live-label states (Q5 was paper fetch bounds):**

```sql
SELECT origin, value, provisional, count(*) FROM labels
GROUP BY origin, value, provisional;
SELECT count(*) FILTER (WHERE origin IN
 ('candidate_sample','reject_sample','legacy_positive','gold')) AS old_origins,
 count(*) FILTER (WHERE provisional) AS provisional FROM labels;
SELECT table_name,column_name,is_nullable,column_default
FROM information_schema.columns WHERE table_schema='public' AND
 ((table_name='papers' AND column_name='revision') OR
  (table_name='labels' AND column_name IN ('origin','provisional')));
```

Only **reader / related / false / 1**. Old origins **0**, provisional **0**. `papers.revision` NOT NULL DEFAULT 1; `labels.provisional` NOT NULL DEFAULT false; origin NOT NULL, no default. Production's one label was last written 2026-10-26 17:16:18 UTC.

**Q7 — reachable historical states and recovery:**

```sql
SELECT count(*) FILTER (WHERE jsonb_typeof(version_history) IS DISTINCT FROM 'array'),
 count(*) FILTER (WHERE corrected_at IS NOT NULL),
 count(*) FILTER (WHERE revision>1), count(*) FILTER (WHERE version>1) FROM papers;
SELECT count(*) FILTER (WHERE source='owner'),
 count(*) FILTER (WHERE source='miss') FROM memberships;
SELECT count(*) FROM step_failures;
```

Results **0 / 0 / 218 / 7625**; owner/miss memberships **0 / 0**; step failures **1**. Owner correction and membership API paths are real producers even though their rows are absent today.

**Q8/Q9 — last-write limits:**

```sql
SELECT count(*), max(reported_at) FROM misses; -- 0 / NULL
SELECT table_name,column_name FROM information_schema.columns
WHERE table_schema='public' AND table_name IN
 ('eval_labels','eval_results','eval_samples','regression')
AND data_type LIKE 'timestamp%'; -- no rows
```

## Counts

Counts are inventory rows, not a deduplicated number of symbols or a measure of code size. Config schema and model-role rows intentionally describe different aspects of the same live feature. A drop recommendation is not approval; unknown manual/external use remains unknown.

| Area | Keep | Drop | Owner to confirm |
| --- | ---: | ---: | ---: |
| Configuration | 33 | 0 | 0 |
| Model roles | 8 | 0 | 0 |
| Evaluation tables and cohorts | 10 | 2 | 0 |
| API routes | 64 | 0 | 6 |
| CLI commands | 7 | 0 | 18 |
| Migration guards | 5 | 5 | 0 |
| Other integrations | 1 | 0 | 3 |
