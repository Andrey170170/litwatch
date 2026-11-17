# Where work waits

Evidence report, 2026-11-09, with the 26-hour live window (2026-11-09 19:45 UTC to 2026-11-10 21:45 UTC, one
nightly included) added on 2026-11-11. It is the measured input to the rework's A1, the Database gate
([`specs/architecture/a1-database-gate.md`](../specs/architecture/a1-database-gate.md)). Source references are pinned
to `ac80cdf`. The scripts and raw samples were kept outside the repository and are not preserved; the tables here are
what they produced.

**In short:** work waits on the nightly's **session run lock**, held across arXiv intake and model batches, not on
the database. Over the 26 hours no session waited on another's lock, the API's health probe stayed at p95 39 ms, and
the longest transaction seen lasted 33 s. The one database-side cost the code shows is a taxonomy edit's settlement
fan-out (up to 6,777 papers in one exclusive transaction), whose duration has not been measured. Connections cost
milliseconds locally.

## Scope and method

- Read `AGENTS.md`, `docs/agents/dev-host.md`, `GLOSSARY.md`, and the October 21 review. No litwatch CLI, tests, migrations, secrets, container environment dumps or configuration contents beyond the schedule section were used. The only repository addition is this requested note.
- Static baseline: `ac80cdf9ef5240bbcf5d3497be38fe043c531709`, the checkout at investigation start. Production image: `litwatch:1.0.0-dev-424d98d`, OCI revision `424d98d5340f621fcbdc90e2d318383d74ceac95`, started November 4 at 17:21:42 UTC. Hashes of deployed Python source match that baseline except `intake/atom.py` and `store/papers.py`. The lock, worker, parsing-call-site and gateway evidence below is present in production; the papers.py difference is documentation and a one-line offset, while atom.py adds freshness handling for new cross-listings; neither changes a lock scope. Another thread advanced the shared checkout to `26b4ab314823567d8610af6feba0b5fdb639543c` (#139, paper withdrawal) during this investigation. **All source line references and scope counts in this note are pinned to ac80cdf**, not the later checkout. Use `git show ac80cdf:<path>` to resolve those references. No other thread's work was changed.
- Historical collection at approximately 19:46 UTC: 96 runs, 60,981 journal rows, six jobs, 1,301 passes and 94,968 outbox events. Coverage begins October 17. Retained service logs cover November 4 17:21 UTC through November 9 19:55 UTC: 76,314 log lines, 15 warnings and no error-level lines.
- All production database connections use `docker exec litwatch-db psql -X ... -U litwatch -d litwatch` over the container's local socket. `PGOPTIONS` makes sessions read-only from connection start; each also executes `SET default_transaction_read_only = on`. Only SELECTs follow. Queries have statement timeouts (3 seconds for sampling, 15 for history) and a one-second lock timeout. No advisory locks are acquired by the investigation.
- Static search plus an AST inventory identifies all **31 explicit transaction/savepoint sites and 75 transaction/change/session-lock context sites** in `src/litwatch`. The resource map and full call-site inventory appear below. Autocommit statements also take ordinary PostgreSQL statement/row/relation locks; the inventory does not imply only explicitly locked statements can wait.
- The sampler uses one persistent psql session, rather than a new database connection every five seconds. It captures database activity, blocked/blocking PIDs and advisory/ungranted/blocker locks every five seconds; queue/cursor/snapshot-holder metadata once per minute; and a no-token async health request every five seconds. SQL string literals are redacted before query text is truncated to 200 characters.
- `history.py` produces SELECT-only aggregates; `analyze.py` is completely offline and regenerates the tables in this note from captured JSON. `static_map.py` only parses source. `connections.py` compares local psql reconnects with a warm session. `logs.py` extracts sanitized warning/scheduler/batch signal. Exact SQL and source hashes were kept beside them.

**Measurement definitions.** Run duration is `finished_at - started_at`. A job's queue delay is its first linked run start at or after the current `queued_at`, minus that timestamp. Journal latency is `responded_at - started_at`: it includes admission, provider semaphore wait, batch fill/polling, retries and response accounting, rather than pure model generation. Cached answers can bypass new journal rows; absence of a reused row is not a cache-hit rate. Pass `tried_at` is overwritten on each claim, so queue-to-try below means **latest claim**, not first admission. Completed passes clear their lease, preventing historical lease-expiry reconstruction. Digest rows are upserted: their timestamps describe the latest recorded write, including a retry, not a complete publication history. Week digests are compared with their actual run start, not 02:00 on the period's first day.

## Findings, ranked by impact

### 1. Hours of external/model waiting occupy the global run lock

**Measured:** 24 nightly runs, median **2,499.006 s (41.65 min)**, p95 **19,953.684 s (5.54 h)**, maximum **20,283.574 s (5.63 h)**. All have finish timestamps and none has a run-level error. This does not establish complete intake or successful downstream steps. The longest run, November 7 (`runs.id=76`), began 09:00:06 UTC and ended 14:38:10 UTC. Its 649 analysis journal rows span 09:03:50–14:32:37; logs record a batch cancellation at **14:30:28 UTC**, the configured 07:30 local deadline. Recent batched analysis latency is p50 **321.718 s**, p95 **19,719.760 s** (4,210 rows). The all-history and last-seven-day role tables below distinguish this changing tail.

**Measured (static):** `jobs/nightly.py:204` holds `run_lock` across intake, classification, analysis, grading, full-text work, waiting for texts, digest composition and gateway shutdown. The lock is session-scoped (`jobs/lock.py:22`); database connections are autocommit (`store/db.py:25`). Models normally run outside `word.change` transactions. November 9 (`run=95`) illustrates several separate waits: first classification at +545.284 s; final analysis reply +991.375 s; final check reply +1,467.937 s; final Brief reply +2,249.427 s; final writer reply +3,250.549 s. Its digests begin being recorded at +2,624.978 s.

**Measured (static), historical effect not demonstrated:** `jobs/worker.py:62,112` refuses every new job chunk while the nightly is due/running, before trying the run lock. `jobs/lock.py:24` uses `pg_try_advisory_lock`, not a blocking lock request. Consequently this waiting can leave **no `pg_blocking_pids` pair and no PostgreSQL Lock wait**. All six recorded jobs are digest retries outside a nightly: queue-to-start 0.012931–13.412277 s, execution 4.992556–11.076431 s, and **zero seconds of queue interval overlapping a nightly**. The observed 7.237/13.412 s waits on November 2 align with earlier digest jobs ending. Do not call the 5.63-hour nightly an observed 5.63-hour owner-job wait.

Gate implication — **Work:** admission delay behind a nightly is distinct from SQL lock wait and provider/batch service time.

### 2. Sequential arXiv timeout/retry work consumes minutes before classification

**Measured:** retained logs contain **12 category OAI timeout warnings and three catalog-pass timeout warnings**, all reporting three attempts with a 30-second read timeout. On November 9, category warnings occur at 09:02:37, 09:04:51 and 09:07:04 UTC, then the catalog warning at 09:09:21. Classification starts 09:09:23, **545.284 seconds after run start**. This is a measured pre-classification interval, not an isolated network-only duration. On November 8, a no-insert nightly still took **359.843 s** amid three category timeouts.

**Measured (static):** `intake/collect.py:95` harvests categories sequentially; synchronous network/XML fetches are awaited through `asyncio.to_thread` at lines 103 and 126. `intake/catalog.py:79` likewise offloads harvesting, then registers outside-category records one HTTP call at a time at line 101. These waits hold the **nightly run lock**, not an open page-write transaction (`intake/batch.py:39`). They should not be labelled synchronous HTTP on the API loop.

Gate implication — **Work:** external intake dependency/retry time is a material component of background occupancy.

### 3. Taxonomy edits have a large, unbounded settlement transaction

**Measured (static); runtime cost remains a hypothesis:** `api/settings.py:101` takes the taxonomy lock exclusively before loading and saving the tree. `feedback/word.py:371` keeps that transaction open while looping over each changed topic and its descendants, then settling tied papers if reordered. `settle_topic` at line 413 visits every affected paper. The same paper can be settled again for another changed topic; nested transactions are savepoints, so they do not release the outer exclusive lock.

Production SELECTs using the same union as `store/word.py:151` find **953, 560 and 986** papers for the three Focus topics; **1,188 and 1,801** for the two Areas; and **6,777 / 1,130 / 2,952 / 2,573** for the Fields. These are per-topic candidate counts, not disjoint counts or measured edits. A language-models topic edit can therefore reach 6,777 settlements before commit. Multiplying the October review's approximate 20 statements by that scope suggests approximately 135,540 statements; that is an illustrative extrapolation, **not a current measured statement count**.

The old structural issue persists: `feedback/word.py:170` loads topics; line 174 loads all historical verdicts for the paper; `_text_pass` at line 264 reloads the whole taxonomy, including topics, for each settlement; `_deep_pass` at line 307 reads further eligibility facts. Each settle also retakes shared taxonomy/paper locks in a nested scope. Readers that call `taxonomy.load` take a shared advisory lock (`store/taxonomy.py:92`), so an exclusive edit can delay reads as well as writers. No model/HTTP/PDF call was found within this exclusive-taxonomy path. Actual edit latency and contention are **unmeasured**; no edit was triggered.

Gate implication — **Database:** transaction/lock occupancy must account for the entire taxonomy-edit settlement fan-out.

### 4. Pass age is substantial, but the records do not identify pure lease wait

**Measured:** 320 completed Briefs have queue-to-completion p50 **939.554 s**, p95 **11,152.900 s**; 837 completed Weight re-checks have p50 **1,151.819 s**, p95 **7,913.353 s**. For 138 completed Deep passes the corresponding values are **286.215 s** and **173,129.671 s (48.09 h)**. Deep queue-to-latest-try p95 is **173,071.694 s**, whereas latest-try-to-finish p95 is **120.178 s**. Separate Deep attempts confirm 132 automatic attempts at p50 **75.058 s**, p95 **120.091 s**, plus six requested attempts at p50 **67.553 s**, p95 **75.076 s**; all ended, none records a service failure.

At the snapshot there are **no running or ready passes and no expired running leases**. One owner-priority Brief has awaited text since November 4 15:58 UTC (about 5.15 days) and is still being retried; five passes have reached three failed runs (one Brief, four Weight re-checks). These are visible waiting/terminal outcomes, not proof of blocked leases. Historical `tried_at` overwrites and cleared `lease_until` prevent separating original admission, retry delay, text availability and lease-recovery delay. No per-attempt claim history exists for ordinary full-text work.

**Measured (static):** ordinary full-text waves have four reads/calls at a time; a batching nightly claims all eligible automatic passes at once (`fulltext/__init__.py:281–316`), with leases extended past its batch deadline. Deep has a separate session worker lock and processes **one pass at a time** (`fulltext/deep.py:90–128`). Background-priority Deep waits behind due/running nightly work; ordinary automatic Deep and owner requests are not all paused by that rule. Normal text passes are restricted to owner priority while another session holds the run lock (`fulltext/__init__.py:295`). These are distinct policies, not one universal full-text/nightly exclusion. The ordinary text lease is 30 min (`core/fulltext.py:29`); Deep's default is 45 min follow timeout plus 15 min margin (`integrations/deep.py:95,209`), configurable rather than verified deployment settings. All idle workers poll at 30 s unless woken; re-runs awaiting text defer by 30 s (`jobs/reruns.py:40`).

Gate implication — **Work:** queue admission, dependency availability, retry eligibility, service execution and lease recovery are different waits.

### 5. Some parsing runs on the API event loop; PDF work already leaves it

**Measured (static); actual event-loop stalls remain a hypothesis:** `intake/collect.py:59–68` synchronously reparses every selected stored Atom feed after its database read. Production stores **72 snapshots / 38,986,606 body bytes**; the largest is **1,880,214 bytes**. A fixed recent window beginning November 6 contains 12 snapshots totalling **4,049,544 bytes**. That quantifies reachable input volume, not elapsed parsing time or the exact next nightly's selection. The parser implementation itself differs between checkout and deployment, but the synchronous call site matches. Existing MCP timing logs contain **14 successful calls**: three `paper_outline` calls at **1.130–1.282 s**, eight `read_paper` calls at **0.061–0.096 s**, two searches and one `get_paper`. These are whole-tool durations including dependencies, not measured parsing time or loop stalls; all occurred outside a nightly.

Other paths on the loop: full-text bibliography regex processing **before** the 160,000-character cap (`fulltext/brief.py:133`, `fulltext/trim.py:14`); full-text hashing (`archive/client.py:50`, called by `fulltext/deep.py:162` before the transaction and by result-recording paths inside changes); whole-LaTeX outline parsing in MCP calls (`api/mcp.py:468,502`, `api/mcp_reading.py:190`); JSON/schema serialization and validation in the gateway (`llm/gateway.py:210,405`); replay offer CSV reads (`jobs/replays.py:54`, `quality/replay.py:73`) and source-file reads for replay fingerprints (`quality/replay.py:166`). Replay offer/make happens inside the replay transaction and exclusive replay advisory lock, with taxonomy shared locks surviving to outer commit (`api/jobs.py:356,372`, `jobs/replays.py:61`). That is a reachable **file-I/O-under-transaction** path. SPA requests also synchronously call `Path.resolve/is_file` (`api/app.py:178`).

The broad PDF-stalls-loop hypothesis is **not supported by the code as stated**: PDF extraction uses `run_in_executor` (`archive/text.py:113`), and MCP page rendering uses a dedicated executor (`api/mcp.py:542`). Both serialize through a shared native-PDF thread lock (`archive/text.py:44,61`, `archive/pages.py:85`), which can cause executor queueing rather than direct event-loop blocking. Source HTTP parsing is largely offloaded (`intake/search.py:205`, `intake/collect.py:103,126`); integration search has its own executor (`api/integrations.py:294`). WebDAV zip compression is synchronous (`integrations/zotero.py:588`, called at `integrations/webdav.py:197`) but WebDAV is not loaded in this deployment. Gateway construction during model runs (`jobs/context.py:140`, `llm/gateway.py:491`) and monitoring's `_unkeyed` (`api/monitor.py:174`) can synchronously read key files through `config.py:361` when a `_FILE` setting is used; this was identified from source only, without inspecting credentials or their settings. Startup-only configuration reads and CLI evaluation-file writes do not establish steady-state API stalls.

Gate implication — **Compute:** parsing, preparation, hashing and executor/native-library queue time need to be distinguishable from async I/O wait.

### 6. SQL work and snapshot fencing can delay outcomes without a lock error

**Measured:** database counters show **570,936 sessions**, **396,826 sequential scans / 6,788,689,823 sequential tuples read** on `inference_calls` (approximately 61k rows currently), and approximately **1.025 TB of cumulative temporary-file writes** in the database snapshot. These cumulative counters survive server restarts; `stats_reset` is null, so the accumulation interval is not established. They do not attribute cost to a statement, nor establish a current latency incident. Research SELECTs contribute a small amount themselves. There is no `pg_stat_statements` extension, general HTTP/API request-duration log, lock-acquisition timer or connection-acquisition timer. MCP tool calls are an exception: `api/mcp.py:238` logs structured `duration_seconds`; their retained timing evidence is extracted separately.

The code offers concrete candidates: `/summary` calls `run_status` and then recomputes budget lines (`api/summary.py:22,36`); budget lines aggregate the month's JSON usage rows (`store/calls.py:253`). Monitoring also aggregates database facts on every request (`api/monitor.py:141`); its endpoints were deliberately not used as the five-second probe. These are **hypotheses for cumulative scan cost**, not an attribution of the temporary files. Long SQL/JSON scans, I/O and repeated reads remain alternatives to lock contention.

**Measured (static):** outbox visibility is fenced by `xid < pg_snapshot_xmin(pg_current_snapshot())` (`store/outbox.py:25,99`), not just commit and row locks. An old transaction anywhere in the same PostgreSQL cluster can withhold delivery even when `pg_blocking_pids` is empty. The sampler records transaction ages and minute-level xmin holders to detect that case.

At the snapshot, loaded integrations **archive, deep, notion** have zero pending events and no cursor failures; their last cursor update followed the last event creation by **0.867 / 1.913 / 0.992 seconds** respectively. These are three last-position observations, not a historical delivery percentile or a timestamp for every handler. Zotero and WebDAV each show 94,968 events after stale zero cursors, but neither appears in the deployed dispatcher's startup list; **do not report those as live delivery backlogs**. The Deep dispatcher still handles no wanted events (`integrations/deep.py:213`), separately from the Deep pass worker.

Gate implication — **Database:** observe statement time and transaction/snapshot lifetime separately from advisory/row lock wait.

### 7. Connection churn is real; large per-connection latency is not established

**Measured:** 20 reconnects in one persistent psql process/docker-exec pipe take p50 **2.794 ms**, max **3.710 ms**, including `SET` and `SELECT 1`; 20 warm `SELECT 1` round trips have p50 **0.140 ms**. Fresh psql processes take about **31.047 ms** each; most of that difference is tool/process startup and must not be called database connection time. This benchmark used the required local socket, not the service's TCP/password-authenticated path. No credentials were read to benchmark that path.

**Measured (static):** every API request dependency opens a connection (`api/deps.py:25`). Scheduler, full-text and Deep ticks open one each (`jobs/scheduler.py:119`, `fulltext/__init__.py:374`, `fulltext/deep.py:283`); workers and integrations also keep some sessions for their whole loops, and model-run journals use siblings (`jobs/context.py:134`). Four application sessions observed in the baseline date from November 4. Thus 'one connection per unit' does not mean every integration poll creates a connection. Session totals cannot be attributed to the API or converted into connection latency. No pool saturation can be measured because no pool exists.

Gate implication — **Database:** connection creation/reuse is measurable, but the permitted benchmark does not establish it as the main source of waiting.

## Hypothesis verdicts

| Hypothesis | Evidence and current verdict |
| --- | --- |
| (a) Locks/long transactions cause the blocking, not async itself | **Not established as a dominant cause.** Hours of external/model waiting under a session run lock are established; exclusive taxonomy fan-out and xmin fencing are real code paths. Historical deadlocks are zero, retained logs have no lock/serialization errors, and the 26-hour window, a nightly included, has no blocked pair. None of these establishes p95 authenticated API latency during a nightly. |
| (b) Exclusive taxonomy settlement is costly | **Reachable fan-out established; duration remains a hypothesis.** Up to 6,777 papers for one topic, repeated per-topic settlement and redundant reads, with one outer exclusive transaction. No production edit was triggered or timed. |
| (c) Run lock serializes jobs behind nightly | **Policy established; historical owner-job penalty unmeasured.** Nightly-wide run-lock ownership plus explicit admission deferral; six actual jobs have zero nightly queue overlap. Try-lock refusals are invisible to blocked-PID sampling. |
| (d) Synchronous PDF/parsing work stalls API loop | **Mixed.** PDF extraction/rendering already use executors; native PDF work is serialized there. Stored-feed/LaTeX parsing and preparation do run on the loop. Their actual stalls need correlation with health latency and the active workload. |
| (e) Per-unit connection opening is noticeably expensive | **Local cost measured, importance unproven.** About 2.8 ms local reconnect round trip versus 0.14 ms warm query; service TCP/auth path unmeasured. External waits are orders of magnitude larger for background duration. |

## Transaction and lock resource map

All paths below are source-relative to `src/litwatch/`. PostgreSQL locks are released at transaction end for xact/row locks and at explicit unlock/session end for session locks. A nested `conn.transaction()` is a savepoint, not a resource-release boundary. A shared lock acquired inside an exclusive owner transaction does not narrow that outer exclusive scope.

| Resource and acquisition | What is held / work inside / wait behavior |
| --- | --- |
| Migration `7213910`, `store/db.py:70,87` | Blocking session lock across numbered migrations; one transaction per migration and bookkeeping. Startup/CLI path, not steady-state requests. DDL/file reads, no model/HTTP. Not invoked here. |
| Run `7213911`, `jobs/lock.py:22`; `jobs/nightly.py:204`; `jobs/worker.py:124`; `jobs/digests.py:34`; CLI `cli.py:117,135,169` | Nonblocking session try-lock. Entire nightly, one owner-job chunk (up to 50 papers), an entire digest window or explicit intake command. **HTTP/model calls, text/PDF work, sleeps/batch waits and long loops while this session lock is held**, usually with no SQL transaction open. |
| Scheduled-kind `(7213913, hashtext(kind))`, `jobs/scheduler.py:86`; `jobs/lock.py:35`; `jobs/publications.py:99` | Nonblocking session try-lock from due-decision through entire job. Publication lookups hold it over paced DBLP HTTP, but do not take the run lock. The scheduler awaits nightly then publications sequentially. |
| Taxonomy `7213913`, `store/taxonomy.py:41,48,92,138,240` | Exclusive transaction lock for save/reorder; shared for taxonomy snapshots and paper changes. Exclusive save/reorder contains class/topic loops plus all paper/tie settlements (`feedback/word.py:371,388`). No HTTP/model call located under the exclusive edit. |
| Paper settlement `(7213912, hashtext(paper))`, `store/word.py:19`; `feedback/word.py:83,110,146` | Blocking transaction advisory locks in sorted paper-id order, taxonomy first. Checks, mutations, recomputation, pass/job eligibility and outbox events before commit. `change` settles each named paper once at outer exit; direct `settle` has its own nested scope. |
| Paper row SHARE, `store/papers.py:273`; UPDATE, `store/papers.py:284`; SHARE, `store/fulltext.py:464` | Revision/dismissal stability through recording or owner correction. Called inside changes/transactions. DB writes and settlement, no model/network work in those caller scopes. |
| Job row UPDATE `store/jobs.py:55,68,355,372,386`; claim SKIP LOCKED `store/jobs.py:178` | Read-modify-adopt/API actions or atomic claim. Claim's row lock ends at autocommit statement completion; ownership continues as `(backend_pid, backend_start)` until finish/session loss (`store/jobs.py:160`). Claiming does not retain that row lock during inference. |
| Re-run `(7213916, hashtext(topic))`, `store/jobs.py:340`; replay `7213917`, `store/jobs.py:365` | Blocking xact locks serialize offer/adoption/creation. Replay offer holds its lock through cohort/source-file reads, taxonomy/label/model-cost reads and preparation (`jobs/replays.py:58`). No HTTP/model execution during the offer transaction. |
| Full-text claim and ownership `store/fulltext.py:277,402` | `FOR UPDATE OF f SKIP LOCKED` in one claim statement; later `held` verifies lease token and locks row for record/judgement. Durable leases cover reading/inference **outside** that row-lock transaction. Retry/take-back transactions at lines 392,753 take taxonomy shared and update eligible rows. |
| Deep worker `7213915`, `store/fulltext.py:259`; `fulltext/deep.py:90` | Nonblocking session lock across the entire serial drain. **Archive HTTP, PDF work, submission/follow polling and long per-pass loop inside this lock**. Short paper-change transactions around judgement/submission metadata/result writes; `deep.submission` at line 174 reads DB metadata only; HTTP submit at line 177 follows commit. |
| Digest row UPDATE `store/digests.py:713`, caller `api/digests.py:236`; recording transaction `store/digests.py:574` | Lock failed digest while queuing/reusing a write job. Publish stores digest/covered-paper rows, settles retry job and emits event. Composition/model call happens **before** `word.change` (`digests/__init__.py:236,238`). |
| Publication `(7213914, hashtext(paper))`, `store/publications.py:40`, `jobs/publications.py:39` | Transaction advisory lock for compare/upsert/event. DBLP lookup is before the write transaction (`jobs/publications.py:65,71`). |
| Dispatch `(7213914, hashtext(integration))`, `store/outbox.py:78`; `events/dispatch.py:57,110,137` | Nonblocking session lock across setup, up to 100 delivered events, or idle hook. **Integration HTTP/file preparation while held**; no encompassing delivery transaction. Independent connection/loop per integration. Failures retain cursor and back off 30 s to 1 h. |
| Retally `(7213912, run_id)`, `store/calls.py:174` | Blocking session advisory lock plus per-journal asyncio mutex across aggregation/update of an old run. No model/HTTP within it. Released after the retally statement, not after the whole inference wave. |
| Intake page `intake/batch.py:39`, `store/papers.py:99` | Up to **500** paper advisory locks, shared taxonomy, per-paper upserts/events, and rewritten-paper settlements. One transaction per page unless called inside an outer change. All network fetching occurs before this transaction. |
| Other explicit transactions: `feedback/calibrate.py:91,158`; `quality/replay.py:306`; `api/settings.py:221`; `fulltext/deep.py:277`; `jobs/worker.py:165,270`; `store/papers.py:314` | Calibration session/item insertion loops; frozen evaluation labels; acknowledgements; Deep failure/lease update; job finalization/pass release; corrected text/event. Resource details and every enclosing call site are in the complete inventory below. |

The paired advisory namespaces are intentionally listed separately by **purpose**: retally and paper-settlement use the same numeric two-key namespace; publication and dispatch likewise. A gate must not infer purpose from the numeric namespace alone. One-key and two-key advisory key spaces are separate.

### In-process locks, admission and blocking-compute boundaries

| Resource / source | Scope and consequence |
| --- | --- |
| `llm/gateway.py:145,228` | Per-gateway, per-provider semaphore (default 16); held over one actual provider attempt. The journal starts before admission; timeout starts after semaphore acquisition. Retry sleeps of 1/5/20 s happen after releasing this semaphore (`:251`). Different gateways do not share one global limiter. |
| `llm/ceiling.py:75,89,102` | Condition protects budget reservations/settlement; `wait()` releases it while awaiting in-flight calls. Admission wait is not a DB wait. |
| `concurrency.py:19,22`; `fulltext/__init__.py:284,287` | Per-operation coroutine width; full-text read semaphore has four permits and is held across archive/PDF reads. A batching wave waits for all text reads before starting that wave's calls (`fulltext/__init__.py:316`). |
| `publications/dblp.py:164,168` | Async mutex spans pacing sleep and HTTP query. Owner BibTeX lookup can await scheduled lookup pacing through the same object; it is not a taxonomy/SQL lock. |
| `integrations/webdav.py:126,212` | Async arXiv mutex spans PDF HTTP and pacing sleep. Currently not loaded. |
| `store/calls.py:47,183` | Per-journal async mutex covers retally session-lock acquisition, aggregate/update and unlock. |
| `archive/text.py:44,61`; `archive/pages.py:85` | Shared native-PDF threading lock across extraction/rendering, in executors. Default pool for normal text reads, single dedicated MCP executor (`api/mcp.py:193`). |
| `api/integrations.py:294,305`; `store/quality.py:233` | Separate integration-search executor and offloaded keyword scan. Existing compute boundaries rather than loop-blocking examples. |
| `quality/classify.py:153,158`; `quality/briefs.py:217,231`; `quality/ladders.py:485,524,528,530,545` | CLI evaluation semaphores, budget condition and output-directory nonblocking flock. Ladders holds flock across calls and synchronous output append loops. Not a background loop in `serve`; no evidence it blocks this production API process. |

### Separate archive boundary

The archive is not a sixth loop in this process: six archive services and a socket are active, including the API, acquisition worker, indexer and Litwatch bridge. litwatch reaches its API over HTTP on the host. No archive credentials, mutable database connection, acquisition or configuration operation was used. Its runtime latency and queue contents were not collected, so text-availability delays cannot be attributed to its storage/provider mechanisms from this investigation alone.

Code boundaries to preserve when interpreting client waits: `archive/src/arxiv_archive/api.py:63` has a four-request semaphore and a two-search semaphore, rejecting overload with 503; `pacing.py:95–125` takes an interprocess flock over the arXiv request and enforces shared cooldown/pacing; `worker.py:207` holds a singleton worker flock; `catalog.py:401` claims a SQLite job with `BEGIN IMMEDIATE` and a default 300-second lease. Store/file-lock boundaries are in `store.py:49`; SQLite catalogue transactions include `catalog.py:145,186,226,241,283,359,381,405,437,448,457,518,539,556`. Search/indexer snapshots and writes live at `search.py:238`, `search_indexer.py:28,98,209`, with singleton flock `search_indexer.py:50`; recovery/campaign transactions and maintenance flocks are in `campaign.py:110`, `recovery.py:76,98,167,220,256`, `backup.py:171`. These are external resource scopes, not PostgreSQL or litwatch-loop waits; no archive-wide exhaustive runtime audit is claimed.

## The 26-hour live window

One persistent read-only psql session sampled every five seconds from 2026-11-09 19:45 UTC to 2026-11-10 21:45 UTC,
across the 2026-11-10 nightly: **18,720 samples**, no malformed lines, no probe errors, no read-only violations.

- **Lock waits: none.** No blocked/blocking pair in any sample.
- **Transactions are short.** Transaction age over 162 samples where one was open: p50 **0.014 s**, p95 **14.4 s**,
  max **33.4 s**. Active query age over 118 samples: p50 **0.003 s**, p95 **3.96 s**, max **15.2 s**.
- **The database is mostly idle.** Backend states: idle 76,054, active 118, idle in transaction 44.
- **Active waits** are ordinary I/O and client writes: CPU/runnable 59, ClientWrite 32, WalSync 13, others under 5
  each (table below).
- **The API stayed responsive.** The no-database health probe answered 18,668 times: p50 **15 ms**, p95 **39 ms**,
  max **1.14 s**. 52 probes failed (51 connection errors, one timeout), most likely the production deploy of
  2026-11-10; not checked.

Five-second sampling misses shorter waits; try-lock refusals (the run lock, the Deep worker's, dispatch) leave no
blocked pair at all, so this window says nothing about owner work refused behind the nightly. A quiet night with no
owner jobs cannot give that percentile.

## Historical tables

Seconds throughout, from SELECT-only aggregates over production history on 2026-11-09.

## Runs

| Kind | n | p50 s | p95 s | max s | errors | open |
| --- | --- | --- | --- | --- | --- | --- |
| digests | 2 | 90.518 | 167.751 | 176.332 | 0 | 0 |
| fulltext | 36 | 18.063 | 32.391 | 86.217 | 0 | 0 |
| grading | 2 | 5.603 | 6.309 | 6.388 | 0 | 0 |
| intake | 2 | 1148.750 | 2178.494 | 2292.910 | 0 | 0 |
| job | 6 | 6.939 | 10.391 | 11.076 | 0 | 0 |
| nightly | 24 | 2499.006 | 19953.684 | 20283.574 | 0 | 0 |
| publications | 24 | 11.179 | 79.610 | 99.069 | 2 | 0 |

## Latest queue episodes

| Job | Kind | queue to first run s | run duration s | nightly overlap s |
| --- | --- | --- | --- | --- |
| 1 | digest | 0.014 | 8.333 | 0 |
| 2 | digest | 7.237 | 8.132 | 0 |
| 3 | digest | 13.412 | 11.076 | 0 |
| 4 | digest | 0.013 | 5.746 | 0 |
| 5 | digest | 4.860 | 4.993 | 0 |
| 6 | digest | 0.028 | 5.674 | 0 |

## Journal latencies, all history

| Role | Batch | n | responded | p50 s | p95 s | max s | error rows |
| --- | --- | --- | --- | --- | --- | --- | --- |
| analyse | False | 6 | 6 | 20.708 | 22.592 | 22.670 | 0 |
| analyse | True | 17477 | 17477 | 373.781 | 618.030 | 19726.423 | 1712 |
| area_check | False | 3020 | 3020 | 1.338 | 147.140 | 148.592 | 0 |
| brief | False | 136 | 136 | 14.772 | 24.201 | 74.266 | 1 |
| brief | True | 1022 | 1022 | 434.518 | 7799.060 | 11103.991 | 7 |
| check | False | 2578 | 2576 | 86.521 | 519.506 | 637.566 | 2 |
| classify | False | 17477 | 17477 | 9.001 | 47.587 | 58.616 | 0 |
| field_grade | False | 428 | 428 | 1.794 | 3.333 | 5.129 | 0 |
| field_grade | True | 13135 | 13135 | 455.269 | 19421.591 | 19422.725 | 0 |
| judge | False | 215 | 215 | 2.281 | 5.716 | 9.016 | 3 |
| judge | True | 5347 | 5347 | 454.232 | 19420.308 | 19423.998 | 12 |
| writer | False | 45 | 45 | 8.037 | 13.427 | 22.068 | 0 |
| writer | True | 95 | 95 | 373.445 | 18156.789 | 18173.159 | 9 |

## Journal latencies, last seven days

| Role | Batch | n | p50 s | p95 s | max s | error rows |
| --- | --- | --- | --- | --- | --- | --- |
| analyse | False | 2 | 16.256 | 21.433 | 22.009 | 0 |
| analyse | True | 4210 | 321.718 | 19719.760 | 19726.423 | 1 |
| area_check | False | 681 | 0.744 | 2.303 | 2.598 | 0 |
| brief | False | 52 | 14.704 | 20.726 | 57.465 | 0 |
| brief | True | 217 | 433.401 | 434.561 | 434.578 | 2 |
| check | False | 592 | 41.289 | 231.172 | 265.617 | 0 |
| classify | False | 4210 | 6.252 | 19.246 | 21.717 | 0 |
| field_grade | False | 428 | 1.794 | 3.333 | 5.129 | 0 |
| field_grade | True | 2559 | 261.298 | 435.336 | 435.432 | 0 |
| judge | False | 209 | 2.292 | 5.693 | 9.016 | 0 |
| judge | True | 1057 | 259.233 | 434.849 | 434.876 | 4 |
| writer | False | 3 | 5.405 | 5.524 | 5.537 | 0 |
| writer | True | 33 | 373.031 | 433.416 | 433.456 | 5 |

## Pass timings (latest claim, not first-claim or pure lease wait)

| Kind | State | n | queue to latest try p50 s | p95 s | queue to complete p50 s | p95 s | latest try to complete p50 s | p95 s |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| brief | awaiting_text | 1 | 445698.574 | 445698.574 | — | — | — | — |
| brief | done | 320 | 183.324 | 4334.241 | 939.554 | 11152.900 | 436.619 | 7823.278 |
| brief | failed | 1 | 171735.835 | 171735.835 | — | — | — | — |
| deep | done | 138 | 201.638 | 173071.694 | 286.215 | 173129.671 | 75.136 | 120.178 |
| weight | done | 837 | 87.224 | 4331.305 | 1151.819 | 7913.353 | 441.208 | 7825.742 |
| weight | failed | 4 | 154242.042 | 170511.744 | — | — | — | — |

## Digest current rows, run to last write

| Period | State | n | p50 s | p95 s | max s |
| --- | --- | --- | --- | --- | --- |
| day | nothing_new | 5 | 2032.594 | 16271.212 | 19776.595 |
| day | written | 95 | 2232.604 | 19897.381 | 19965.543 |
| week | written | 36 | 3622.185 | 19883.289 | 19886.254 |

## Outbox cursors

| Integration | Pending | last cursor timestamp minus last event s | failures |
| --- | --- | --- | --- |
| archive | 0 | 0.867 | 0 |
| deep | 0 | 1.913 | 0 |
| notion | 0 | 0.992 | 0 |
| webdav | 94968 | — | 0 |
| zotero | 94968 | — | 0 |

## Retained MCP tool timings

| Tool | n | p50 s | p95 s | max s | error outcomes |
| --- | --- | --- | --- | --- | --- |
| get_paper | 1 | 0.038 | 0.038 | 0.038 | 0 |
| paper_outline | 3 | 1.233 | 1.277 | 1.282 | 0 |
| read_paper | 8 | 0.071 | 0.090 | 0.096 | 0 |
| search_papers | 2 | 0.143 | 0.148 | 0.149 | 0 |

## Live health (HTTP 200 only), the 26-hour window

| n | p50 s | p95 s | max s |
| --- | --- | --- | --- |
| 18668 | 0.015 | 0.039 | 1.145 |

## Live active waits (backend sample counts)

| Wait | Samples |
| --- | --- |
| CPU/runnable / none | 59 |
| Client / ClientRead | 2 |
| Client / ClientWrite | 32 |
| IO / DataFileRead | 3 |
| IO / WalSync | 13 |
| IPC / BgworkerShutdown | 3 |
| IPC / MessageQueueInternal | 2 |
| IPC / MessageQueueReceive | 1 |
| LWLock / WALWrite | 2 |
| Timeout / VacuumDelay | 1 |

## Live state (backend sample counts)

| State | Samples |
| --- | --- |
| idle | 76054 |
| active | 118 |
| idle in transaction | 44 |
| none recorded | 4 |

Idle query age is not query runtime. Observer sessions are excluded from activity counts.


## Complete service transaction/change/session-lock call-site inventory

Generated from pinned ac80cdf by `static_map.py`. Resource mechanics are in the map above; the calls here state what runs inside each lexical scope. Nested rows overlap their enclosing scope; they are not additional independent transactions. Ordinary statement locks, row-lock acquisition primitives, Python semaphores/conditions and external archive resources are listed separately above.

| Source | Scope / held resource | Inside |
| --- | --- | --- |
| `src/litwatch/analysis/__init__.py:148` `record` | `word.change`: shared taxonomy + ordered paper locks; transaction or nested savepoint; settle at outer exit | `events.emit`, `failures.failed`, `failures.succeeded`, `papers_store.still_current`, `store.add`, `store.current_gist_id` |
| `src/litwatch/api/digests.py:236` `write_again` | `c.transaction`: transaction/savepoint; resource details in map above | `jobs_store.write_digest`, `store_.get`, `taxonomy_store.load` |
| `src/litwatch/api/integrations.py:212` `request_card` | `c.transaction`: transaction/savepoint; resource details in map above | `deep_passes.request`, `failures.waits_on_owner`, `fulltext_store.open_deep`, `jobs_store.process`, `jobs_store.processing`, `misses.keep`, `papers_store.dismissed_at`, `taxonomy_store.load`, `word.lock_many`, `word_store.in_collection` |
| `src/litwatch/api/integrations.py:227` `request_card` | `word.change`: shared taxonomy + ordered paper locks; transaction or nested savepoint; settle at outer exit | `deep_passes.request`, `failures.waits_on_owner`, `jobs_store.process`, `misses.keep`, `taxonomy_store.load`, `word_store.in_collection` |
| `src/litwatch/api/jobs.py:152` `process` | `c.transaction`: transaction/savepoint; resource details in map above | `failures.waits_on_owner`, `jobs_store.process`, `jobs_store.process_of`, `misses.keep`, `papers_store.dismissed_at`, `taxonomy_store.load`, `word.lock_many`, `word_store.in_collection` |
| `src/litwatch/api/jobs.py:165` `process` | `word.change`: shared taxonomy + ordered paper locks; transaction or nested savepoint; settle at outer exit | `failures.waits_on_owner`, `jobs_store.process`, `misses.keep`, `papers_store.dismissed_at`, `taxonomy_store.load`, `word_store.in_collection` |
| `src/litwatch/api/jobs.py:292` `rerun` | `c.transaction`: transaction/savepoint; resource details in map above | `_above_spent`, `jobs_store.create`, `jobs_store.move`, `jobs_store.open_rerun` |
| `src/litwatch/api/jobs.py:356` `replay_offer` | `c.transaction`: transaction/savepoint; resource details in map above | `_offer` |
| `src/litwatch/api/jobs.py:372` `run_replay` | `c.transaction`: transaction/savepoint; resource details in map above | `_offer`, `replays.make` |
| `src/litwatch/api/jobs.py:413` `act` | `c.transaction`: transaction/savepoint; resource details in map above | `_above_spent`, `failures.waits_on_owner`, `fulltext_store.release_job`, `jobs_store.get`, `jobs_store.move`, `jobs_store.open_replay`, `word.lock_many` |
| `src/litwatch/api/reading.py:816` `react` | `word.change`: shared taxonomy + ordered paper locks; transaction or nested savepoint; settle at outer exit | `_known`, `_topic`, `word_store.set_reaction` |
| `src/litwatch/api/reading.py:832` `unreact` | `word.change`: shared taxonomy + ordered paper locks; transaction or nested savepoint; settle at outer exit | `_shown`, `word_store.clear_reaction`, `word_store.reactions_for` |
| `src/litwatch/api/reading.py:846` `save` | `word.change`: shared taxonomy + ordered paper locks; transaction or nested savepoint; settle at outer exit | `_known`, `word_store.set_hand` |
| `src/litwatch/api/reading.py:857` `label` | `word.change`: shared taxonomy + ordered paper locks; transaction or nested savepoint; settle at outer exit | `_known`, `_topic`, `feedback_store.set_label` |
| `src/litwatch/api/reading.py:883` `report_miss` | `word.change`: shared taxonomy + ordered paper locks; transaction or nested savepoint; settle at outer exit | `_topic`, `misses.keep`, `misses.report` |
| `src/litwatch/api/settings.py:101` `save_topic` | `c.transaction`: transaction/savepoint; resource details in map above | `taxonomy_store.load`, `taxonomy_store.lock`, `word.save_taxonomy` |
| `src/litwatch/api/settings.py:221` `acknowledge` | `c.transaction`: transaction/savepoint; resource details in map above | `digests_store.acknowledge`, `failures.acknowledge`, `fulltext_store.acknowledge`, `integrations_store.acknowledge_notes` |
| `src/litwatch/api/settings.py:343` `_awaiting` | `word.change`: shared taxonomy + ordered paper locks; transaction or nested savepoint; settle at outer exit | `failures.waiting`, `papers_store.lock` |
| `src/litwatch/api/settings.py:357` `dismiss` | `_awaiting`: shared taxonomy + ordered paper locks; transaction or nested savepoint; settle at outer exit | `papers_store.dismiss` |
| `src/litwatch/api/settings.py:371` `classify_by_hand` | `_awaiting`: shared taxonomy + ordered paper locks; transaction or nested savepoint; settle at outer exit | `classification.by_hand`, `taxonomy_store.load` |
| `src/litwatch/api/settings.py:399` `correct` | `_awaiting`: shared taxonomy + ordered paper locks; transaction or nested savepoint; settle at outer exit | `papers_store.correct` |
| `src/litwatch/api/settings.py:406` `retry_analysis` | `_awaiting`: shared taxonomy + ordered paper locks; transaction or nested savepoint; settle at outer exit | `failures.succeeded` |
| `src/litwatch/api/settings.py:427` `grade_now` | `word.change`: shared taxonomy + ordered paper locks; transaction or nested savepoint; settle at outer exit | `failures.reset`, `taxonomy_store.load` |
| `src/litwatch/api/settings.py:434` `grade_now` | `_awaiting`: shared taxonomy + ordered paper locks; transaction or nested savepoint; settle at outer exit | `failures.reset` |
| `src/litwatch/api/settings.py:481` `_run_now` | `word.change`: shared taxonomy + ordered paper locks; transaction or nested savepoint; settle at outer exit | `fulltext_store.run_now` |
| `src/litwatch/classification/__init__.py:152` `record` | `word.change`: shared taxonomy + ordered paper locks; transaction or nested savepoint; settle at outer exit | `_announce`, `failures.failed`, `failures.succeeded`, `papers_store.still_current`, `store.add`, `store.replace` |
| `src/litwatch/classification/__init__.py:195` `by_hand` | `word.change`: shared taxonomy + ordered paper locks; transaction or nested savepoint; settle at outer exit | `_announce`, `failures.succeeded`, `papers_store.still_current`, `store.add`, `store.put_owner`, `store.replace`; 1 lexical loop(s) |
| `src/litwatch/cli.py:117` `work` | `run_lock`: session run lock (not one long SQL transaction) | `intake_job.run` |
| `src/litwatch/cli.py:135` `work` | `run_lock`: session run lock (not one long SQL transaction) | `intake_job.catalog_from` |
| `src/litwatch/cli.py:169` `work` | `run_lock`: session run lock (not one long SQL transaction) | `intake_job.from_archive` |
| `src/litwatch/digests/__init__.py:238` `write` | `word.change`: shared taxonomy + ordered paper locks; transaction or nested savepoint; settle at outer exit | `store_.record` |
| `src/litwatch/events/dispatch.py:57` `deliver` | `outbox.dispatching`: per-integration session dispatch lock | `integration.handle`, `outbox.advance`, `outbox.after`, `outbox.cursor`, `outbox.failed`; 1 lexical loop(s) |
| `src/litwatch/events/dispatch.py:110` `start` | `outbox.dispatching`: per-integration session dispatch lock | `outbox.cursor`, `outbox.setup_done`, `outbox.setup_failed`, `setup` |
| `src/litwatch/events/dispatch.py:137` `idle` | `outbox.dispatching`: per-integration session dispatch lock | `hook` |
| `src/litwatch/feedback/calibrate.py:91` `draw` | `conn.transaction`: transaction/savepoint; resource details in map above | `_add`, `conn.execute`, `cursor.fetchone`; 1 lexical loop(s) |
| `src/litwatch/feedback/calibrate.py:158` `more` | `conn.transaction`: transaction/savepoint; resource details in map above | `conn.execute`, `cursor.fetchone`; 1 lexical loop(s) |
| `src/litwatch/feedback/calibrate.py:213` `answer` | `word.change`: shared taxonomy + ordered paper locks; transaction or nested savepoint; settle at outer exit | `_all`, `conn.execute`, `cursor.fetchone`, `taxonomy_store.topics`, `word.label`; 1 lexical loop(s) |
| `src/litwatch/feedback/misses.py:84` `report` | `word.change`: shared taxonomy + ordered paper locks; transaction or nested savepoint; settle at outer exit | `classifications_store.put_owner`, `conn.execute`, `cursor.fetchone`, `papers_store.get`, `taxonomy_store.load`, `word.label`, `word_store.set_hand` |
| `src/litwatch/feedback/word.py:127` `change` | `conn.transaction`: transaction/savepoint; resource details in map above | caller body / yield |
| `src/litwatch/feedback/word.py:131` `change` | `conn.transaction`: transaction/savepoint; resource details in map above | `lock_many`, `settle`; 1 lexical loop(s) |
| `src/litwatch/feedback/word.py:167` `settle` | `conn.transaction`: transaction/savepoint; resource details in map above | `_deep_pass`, `_text_pass`, `analyses_store.current_weight`, `events.emit`, `feedback_store.labels_for`, `fulltext_store.owners_first`, `jobs_store.processing`, `jobs_store.settle_process`, `lock_many`, `taxonomy_store.topics`, `verdicts_store.for_paper`, `word_store.effective_for`, `word_store.in_collection`, `word_store.library_row`, `word_store.rarity_of`, `word_store.reactions_for`, `word_store.set_effective`, `word_store.set_rarity`, `word_store.set_saved`; 2 lexical loop(s) |
| `src/litwatch/feedback/word.py:335` `label` | `change`: shared taxonomy + ordered paper locks; transaction or nested savepoint; settle at outer exit | `feedback_store.set_label` |
| `src/litwatch/feedback/word.py:341` `unlabel` | `change`: shared taxonomy + ordered paper locks; transaction or nested savepoint; settle at outer exit | `feedback_store.delete_label` |
| `src/litwatch/feedback/word.py:347` `react` | `change`: shared taxonomy + ordered paper locks; transaction or nested savepoint; settle at outer exit | `word_store.set_reaction` |
| `src/litwatch/feedback/word.py:354` `unreact` | `change`: shared taxonomy + ordered paper locks; transaction or nested savepoint; settle at outer exit | `word_store.clear_reaction` |
| `src/litwatch/feedback/word.py:361` `toggle` | `change`: shared taxonomy + ordered paper locks; transaction or nested savepoint; settle at outer exit | `word_store.set_hand` |
| `src/litwatch/feedback/word.py:371` `save_taxonomy` | `conn.transaction`: transaction/savepoint; resource details in map above | `settle_ties`, `settle_topic`, `taxonomy_store.save`; 1 lexical loop(s) |
| `src/litwatch/feedback/word.py:388` `reorder` | `conn.transaction`: transaction/savepoint; resource details in map above | `settle_ties`, `taxonomy_store.set_order` |
| `src/litwatch/fulltext/__init__.py:151` `record` | `word.change`: shared taxonomy + ordered paper locks; transaction or nested savepoint; settle at outer exit | `_add`, `store_.awaiting`, `store_.dismissed`, `store_.failed`, `store_.held`, `store_.retry_asked`, `store_.wait_again` |
| `src/litwatch/fulltext/__init__.py:221` `still_wanted` | `word.change`: shared taxonomy + ordered paper locks; transaction or nested savepoint; settle at outer exit | `judged` |
| `src/litwatch/fulltext/deep.py:69` `request` | `word.change`: shared taxonomy + ordered paper locks; transaction or nested savepoint; settle at outer exit | `events.emit`, `store_.deep_facts`, `store_.request_card` |
| `src/litwatch/fulltext/deep.py:90` `work` | `store_.deep_worker`: session Deep worker lock | `_work` |
| `src/litwatch/fulltext/deep.py:163` `_work_one` | `word.change`: shared taxonomy + ordered paper locks; transaction or nested savepoint; settle at outer exit | `cards.add_attempt`, `deep.submission`, `judged`, `store_.submitted` |
| `src/litwatch/fulltext/deep.py:207` `_work_one` | `word.change`: shared taxonomy + ordered paper locks; transaction or nested savepoint; settle at outer exit | `cards.attempt_ended`, `cards.put_card`, `events.emit`, `store_.done`, `store_.held`, `store_.read_of` |
| `src/litwatch/fulltext/deep.py:258` `_ended` | `word.change`: shared taxonomy + ordered paper locks; transaction or nested savepoint; settle at outer exit | `cards.attempt_ended`, `cards.card_failed`, `store_.failed`, `store_.held`, `store_.read_of`, `store_.wait_again` |
| `src/litwatch/fulltext/deep.py:277` `_service_failed` | `conn.transaction`: transaction/savepoint; resource details in map above | `cards.service_failed`, `store_.let_go` |
| `src/litwatch/grading/__init__.py:286` `record` | `word.change`: shared taxonomy + ordered paper locks; transaction or nested savepoint; settle at outer exit | `analyses_store.current_weight`, `events.emit`, `failures.failed`, `failures.succeeded`, `papers_store.still_current`, `store.add`, `taxonomy_store.topics` |
| `src/litwatch/intake/batch.py:39` `store` | `conn.transaction`: transaction/savepoint; resource details in map above | `papers_store.upsert`, `word.lock_many`, `word.settle_all` |
| `src/litwatch/jobs/digests.py:34` `window` | `run_lock, model_run`: session run lock (not one long SQL transaction) | `digests.retry_failed`, `digests.write_day`, `digests.write_week`, `taxonomy_store.load`; 1 lexical loop(s) |
| `src/litwatch/jobs/nightly.py:204` `run` | `run_lock, model_run`: session run lock (not one long SQL transaction) | `_steps`, `intake.collect`, `run.gateway.close`, `run.gateway.settle`, `runs.update` |
| `src/litwatch/jobs/publications.py:39` `store_record` | `conn.transaction`: transaction/savepoint; resource details in map above | `events.emit`, `store.put` |
| `src/litwatch/jobs/publications.py:99` `run` | `job_lock, run_record`: per-kind session lock (not one long SQL transaction) | `look_up`, `store.due`; 1 lexical loop(s) |
| `src/litwatch/jobs/worker.py:124` `step` | `run_lock`: session run lock (not one long SQL transaction) | `self._next` |
| `src/litwatch/jobs/worker.py:165` `_end` | `self.conn.transaction`: transaction/savepoint; resource details in map above | `failures.open_on`, `jobs_store.finish`, `papers_store.dismissed_at`, `word.lock_many` |
| `src/litwatch/jobs/worker.py:270` `_end_rerun` | `self.conn.transaction`: transaction/savepoint; resource details in map above | `fulltext_store.release_job`, `jobs_store.finish` |
| `src/litwatch/quality/replay.py:306` `start` | `conn.transaction`: transaction/savepoint; resource details in map above | `store.put_labels`, `store.start` |
| `src/litwatch/store/db.py:87` `migrate` | `conn.transaction`: transaction/savepoint; resource details in map above | `conn.execute` |
| `src/litwatch/store/digests.py:574` `record` | `conn.transaction`: transaction/savepoint; resource details in map above | `conn.execute`, `cur.executemany`, `cursor.fetchone`, `events.emit`, `jobs.settle_digest` |
| `src/litwatch/store/fulltext.py:392` `take_back` | `conn.transaction`: transaction/savepoint; resource details in map above | `conn.execute`, `lock_shared` |
| `src/litwatch/store/fulltext.py:753` `retry_failed` | `conn.transaction`: transaction/savepoint; resource details in map above | `conn.execute`, `lock_shared` |
| `src/litwatch/store/papers.py:99` `upsert` | `conn.transaction`: transaction/savepoint; resource details in map above | `conn.execute`, `cur.execute`, `cur.fetchone`, `cursor.fetchall`, `events.emit`; 1 lexical loop(s) |
| `src/litwatch/store/papers.py:314` `correct` | `conn.transaction`: transaction/savepoint; resource details in map above | `conn.execute`, `events.emit` |
| `src/litwatch/store/taxonomy.py:92` `load` | `conn.transaction`: transaction/savepoint; resource details in map above | `conn.execute`, `cursor.fetchall`, `lock_shared`, `topics` |
| `src/litwatch/store/taxonomy.py:138` `save` | `conn.transaction`: transaction/savepoint; resource details in map above | `_add_version`, `_latest`, `conn.execute`, `cursor.fetchall`, `gone.fetchall`, `lock`, `retired.fetchall`; 2 lexical loop(s) |
| `src/litwatch/store/taxonomy.py:240` `set_order` | `conn.transaction`: transaction/savepoint; resource details in map above | `conn.execute`, `cursor.fetchall`, `lock`; 1 lexical loop(s) |
