# Deep: open web, traces per attempt, process notes

Approved by the owner on 2026-10-20. This is the reference for three changes to Deep cards:
- the `deep` service's `fetch` reaches any public site behind a network guard;
- every Deep attempt keeps its trace, read through a CLI built for the agent;
- the card says when Deep's own process held it back, in process notes.

What litwatch does is the spec, [`docs/specs/v1/`](../specs/v1/README.md). This plan amended:
- [integrations.md](../specs/v1/integrations.md) (deep);
- [data.md](../specs/v1/data.md) (`deep_cards`, `deep_attempts`);
- [DESIGN.md](../specs/v1/DESIGN.md) (the ladder's Deep card, Needs you);
- [decisions.md](../specs/v1/decisions.md) (80–82);
- [milestones.md](../specs/v1/milestones.md);
- the [glossary](../../GLOSSARY.md) (process note).

Where this plan and the spec differ, the spec wins and this plan is corrected. Terms are the glossary's.

## Goal and approach

**The trigger.** On 2026-10-20 the owner read a Deep card (2610.24495) with this limitation: "The linked
anonymous.4open.science repository could not be checked through the available allowlisted fetch tool, so code
availability is unverified."

**The allowlist.** The agent's `fetch` only reaches a fixed host list in code: `ALLOWED_HOSTS` in `deep/src/hosts.ts`,
covering arXiv, Semantic Scholar, OpenReview, GitHub and Hugging Face. Nothing records why the list exists.

**How often it bites.**
- In production, 5 of 46 cards (11%) said code was unavailable or unverified only because an anonymous.4open.science
  fetch was blocked. All five repositories exist and are public.
- 0.14's cards had 3 more such cases and about 10 blocked project pages: `*.github.io` and personal sites.
- Allowing the host alone would not help. The `/r/<id>` link a paper gives redirects to an API URL that answers
  401. The JSON listing (`/api/repo/<id>/files/`) answers 200.

**The owner's position.** The agent runs in a V8 isolate with no filesystem, no processes and no network of its own,
and holds no tokens. So who cares what it reads?

**The one real danger.** The `fetch` runs in the `deep` container, which reaches the home network. On 2026-10-20 the
router (192.168.0.1) and the host's status endpoint both answered from inside it. The approach:
- **Open the web, wall off the LAN in code.** Any public http(s) host, GET only, with private addresses refused where
  the socket connects and on every redirect hop.
- **Make degraded runs visible.** The model says in process notes when its tools held it back, and the service adds
  notes for what it can detect itself. The notes sit folded on the card and in a quiet group on Needs you.
- **Keep what Deep did.** One trace per attempt, kept for good, read by the agent through a CLI. The owner wants no
  trace UI.
- **Raise effort.** Deep moves from `low` to `medium`: it is cheap on the subscription, a quality bump, and gives
  the trace reasoning to read.

## Choices made without asking

### The network guard (P1)

- **Our own guard, about 40 lines, on `undici` plus `ipaddr.js`.**
  - An `undici` `Agent` with a `connect.lookup` hook resolves with `dns.lookup(host, {all: true})`. It refuses
    unless every address's `ipaddr.process(a).range()` is `"unicast"`; `process` unwraps IPv4-mapped IPv6.
  - The hook checks the very addresses the socket connects to, so DNS rebinding can't slip between a check and the
    connection.
  - **The policy is explicit, not only the library's.** An address passes only if `ipaddr.js` (pinned, 2.5 or
    later) classes it `unicast` after `process()`. Everything else is refused: private, loopback, link-local,
    unique-local, deprecated site-local, carrier-grade NAT, reserved, multicast, and the transition forms that embed
    an IPv4 address (NAT64 `64:ff9b::/96`, 6to4, Teredo). The tests pin each class, as a literal and as a DNS answer
    from a stubbed lookup.
  - Node skips `lookup` for IP-literal hosts. So on every hop the URL's hostname has its IPv6 brackets stripped
    (`new URL("http://[::1]/").hostname` is `[::1]`, and `net.isIP` reads that as 0), and any `net.isIP` literal
    gets the same check up front. WHATWG URL already turns decimal and hex IPv4 forms into dotted ones.
  - `fetch` and `Agent` both come from `undici`, never mixed with Node's bundled fetch.
  - The ready-made SSRF libraries (`request-filtering-agent`, `ssrf-req-filter`) are `http.Agent`s, which Node's
    fetch does not take.
- **The guard covers only the agent's `fetch`.** `store.search` and `s2.lookup` keep `getJson`: litwatch sits at a
  private address (`http://litwatch:8080`).
- **Fetch rules.**
  - GET only, our own user-agent, no cookies, no headers the model can set.
  - `redirect: "manual"`, with a hop loop of at most 5 hops. Each hop re-checks the scheme (http or https), applies
    the rewrite below and is recorded.
  - The body is read as a stream and cut at 2 MB. At most `DEEP_MAX_FETCH_CHARS` (200k) reaches the model, as today.
    A PDF body is cancelled, not read.
  - A non-2xx answer stays a value, as today. A 404 on a repository is the model's call to make, not an error.
- **Budget.** 60 fetches per paper, counted per `buildHosts` call (one per paper); production averages about 5.5 host
  calls per card, 19 at most. The 61st fetch throws in the cell.
- **Pacing.** The pacing lanes keep arXiv 3 s and S2 1.1 s, and every other host gets 1 s. Lanes live with the
  paper's `buildHosts`, so they go when the paper ends.
- **anonymous.4open.science.** `/r/<id>` becomes `/api/repo/<id>/files/`. `/r/<id>/<path>` becomes
  `/api/repo/<id>/file/<path>`, or `files/?path=<path>` when the path ends in `/`. The rewrite applies on every hop.
- **Prompt.**
  - The allowed-host list (`prompt.ts`) becomes: any public page can be fetched; pages built by JavaScript may come
    back empty, so prefer a site's API or raw files.
  - "Checked, not assumed: an unreachable or empty repository is not available" stays.
- **Effort.** `LITWATCH_DEEP_EFFORT` goes from `low` to `medium` in the stack at P1's deploy, so the remade cards get
  it. The 30-minute wall clock is far above the longest run so far (83 s).
- **README.** `deep/README.md` gains the fetch policy and the new settings: budget, hops, byte cap.

### Traces (P2)

- **Migration 0035.**
  - Adds `deep_attempts.trace jsonb`.
  - Copies each card's trace onto the attempt that made it. `card_failed` overwrites the trace with a failed
    attempt's and keeps the older card. So for a card whose `error` is set, the trace is the paper's latest ended
    attempt with that error. Otherwise, it is the latest ended attempt with the card's `pass_id` and no error.
  - The migration fails, leaving the column in place, if any non-empty trace has no attempt or more than one. That
    case is then handled by hand. On the production copy, all 46 cards match exactly one attempt (no failed remake
    yet). P1's remakes come before P2, so the check runs again on a fresh production copy when P2 is built.
  - Drops `deep_cards.trace` only after the copy and the check.
- **Writes.** `attempt_ended` writes the trace. `put_card` and `card_failed` stop touching it. A lost or stuck job has
  no trace (null), not `[]`: `DeepClient`'s `Outcome` carries `trace: list | None` instead of turning a missing
  trace into `[]`.
- **What a trace records, beyond today.**
  - A failed host call keeps its reason (the error message, cut to 300 characters). Today it keeps only `ok: false`.
  - A fetch records its hops, the final URL and status, and the bytes read.
  - A cell that timed out is marked (`timedOut` on `CellResult`), not only described in its error text. A timeout
    ends the session: every later cell is refused, as today.
- **Reasoning.** At `medium` effort the provider's reasoning summaries carry content. If pi-agent-core can pass
  `reasoningSummary`, `detailed` is requested; it costs nothing extra. Otherwise `auto` stays, and P2's PR says so.
- **Retention.** All traces are kept, at about 10 kB each.
- **The agent's CLI.** A Typer group `deep` in `cli.py`, read-only (`_run(..., migrate=False)`):
  - `litwatch deep trace <paper> [--attempt N] [--json]`: the attempts, then the chosen one's turns, cells (code and
    output, cut), host calls with reason and timing, submits and notes. The latest attempt by default.
  - `litwatch deep notes [--since DATE]`: process notes across cards, newest first, with their paper.

  On production they run as `docker exec litwatch litwatch deep …`, which `docs/agents/dev-host.md` records. No trace
  UI for the owner.

### Process notes (P3)

- **Where they live.** In the card, as `process_notes: [{kind, source, note, url?}]`.
  - `kind` is `access`, `tool`, `budget` or `input`.
  - `source` is `model` or `service`.
  - The field is optional; an empty or absent list means no notes.
- **The model's notes.** The schema takes up to 6, each at least 20 characters, with `source` set to `model`. The
  prompt's Standards paragraph says what they are for: what the agent could not do because of its tools, access or
  budget, not what the paper lacks.
- **The service's notes.** `processPaper` collects them during the run and adds them to the returned card when the
  run is over, after `validate` (which sets `additionalProperties: false`), so the model can neither forge nor drop
  them, and events after the submit are included. One note per kind of event per
  run, with a count:
  - a fetch the network guard refused;
  - a fetch that failed on the network (DNS, connection, timeout, a cut body), never for an HTTP status;
  - the fetch budget used up;
  - a cell timeout, which ends the session.

  A run that ends without a card is a failure, handled as today: the paper's failure on Needs you, the service's
  pausing the queue.
- **On the card.** In `DeepCard.tsx`, `process_notes` joins `CORE`. A "Process notes · N" rung follows Limitations,
  folded by default with the existing `button.tpfold` pattern. Each note shows its kind as a tag, and a URL as a link.
- **On Needs you.** A "Deep notes" group, one item per paper (D2): the title, then its notes as short lines.
  - The tone is neutral, neither yellow nor red.
  - It counts nowhere that says something waits: not in `stopped()` or the run status, not in the summary's
    `waiting`, not in the page's "N things wait". Its own head shows its count, like every group, and its
    acknowledged items count in "Acknowledged · N".
  - It has Acknowledge, joins "Acknowledge all", and moves to the folded Acknowledged section like the others. In
    `web/src/lib/settings.ts`, membership in "Acknowledge all" and the Acknowledged section is kept apart from the
    `waiting()` sum. The Acknowledged section shows even when its only items are Deep notes.
  - **The acknowledgement names the card it was given for**, as the other groups name their failure episode
    (pitfalls P16). It is stored in `deep_cards.notes_acknowledged_at` (migration 0036). The request carries each
    paper with the card's `written_at`, and the store only marks a card whose `written_at` still matches. A newer
    card's notes stay unseen even from a stale page. `put_card` clears the mark.
- **Out of Zotero and Notion.** Their whitelists leave `process_notes` out: the notes describe the process, not the
  paper.

### Shipping

- Each stage is one PR through `drive` at land.
- `deep` and litwatch deploy together from one commit, as the stack README says.
- After P1's deploy, the 5 production cards (2610.17976, 2610.23670, 2610.24495, 2610.24872, 2610.26551) are remade
  through the paper page's "Write again".

## Structure

```text
cell: fetch(url)
  budget (60/paper) → 4open rewrite → undici fetch, redirect: manual
    connect.lookup: every resolved address public unicast, or refuse   (literals checked up front)
    hop loop ≤ 5: re-check scheme, rewrite, record hop
  stream ≤ 2 MB → text ≤ 200k chars → cell

process_notes = model's (validated with the card) + service's (merged after validate)
  → card jsonb → DeepCard rung "Process notes · N" (folded)
               → Needs you "Deep notes" (one item per paper, neutral, counts nowhere)
trace → deep_attempts.trace (one per attempt) → litwatch deep trace / deep notes
```

## Decisions

- **D1, how much of the model's reasoning traces capture: raise effort to medium.**
  - The owner: it's rather cheap, so medium is plenty and a good bump for quality.
  - `detailed` summaries are requested as well where Pi can pass them.
- **D2, one item in Needs you's Deep notes group: one per paper.** The title, then its notes. Acknowledge covers the
  paper, and the list stays short when one paper has several notes.

The owner settled four more in conversation:
- the home network is walled off;
- topic names, subjects and rubrics may leave in a fetched URL;
- injected page text can steer a card, as a paper's own text already can;
- process notes appear on the card, folded, and quietly on Needs you, from both the model and the service.

## Risks

- **R1, the fetch reaches the home network.** It does today from the container. The lookup guard, the literal check
  and the re-check on every hop close it.
  - Tests cover private, loopback, link-local, IPv4-mapped and literal addresses, and a public URL redirecting to a
    private one.
  - After deploy, a fetch from the container shows a public site answering and 192.168.0.1 refused.
- **R2, injected page text.** A page can steer what a card says, and anything in context can leave in a URL: the
  paper, the topics and rubrics, search hits. The owner accepted both. The isolate holds no tokens and can write
  nowhere, and process notes make a degraded run visible.
- **R3, noisy notes.** If every dead link became a note, the group would be noise.
  - Service notes skip HTTP statuses, and the group counts nowhere.
  - **Follow-up:** one week after P3 is deployed, read `litwatch deep notes` and tune the prompt or the triggers.
    milestones.md carries this as open from P3's deploy until it is done.
- **R4, the trace move loses some.** The migration copies before it drops. On a sandbox copied from production,
  counts before and after show all 46 traces on their attempts.
- **R5, no gold set for a prompt change.** verification.md asks for one, and it doesn't exist yet (a known gap).
  Instead, P1's PR shows real runs on a dev `deep`: the 5 unverified papers, plus 3 cards that were fine before,
  compared with their production cards.

## Stages

| Stage | Builds | Shows before the next starts |
| --- | --- | --- |
| P1, network | In `deep/`: the guard, hops, budget, byte cap, pacing, the 4open rewrite, prompt and README. Effort `medium` in the stack | Deep tests and typecheck pass. On a dev `deep`, 2610.24495's card finds its repository. Deployed, with the container's fetch refused for 192.168.0.1. The 5 cards are remade |
| P2, traces | Migration 0035, traces per attempt, host failure reasons, hops, cell timeouts, reasoning summaries, the `litwatch deep` CLI, dev-host.md | The migration on a production-copy sandbox with counts. `litwatch deep trace` prints a real trace. Pytest passes |
| P3, process notes | The schema field and prompt, service notes, migration 0036, the card rung, the Needs you group with Acknowledge | Deep tests (fake provider) pass. A real run writes a note. Screenshots of the card and Needs you. Run-status counts unchanged |

## Verification

| Claim | Evidence |
| --- | --- |
| V1. Any public host can be fetched; no private address can, directly or through a redirect | `deep/tests/hosts.test.ts` against a local server and a stubbed lookup: every refused class (private, loopback, link-local, unique-local, site-local, CGNAT, reserved, NAT64, 6to4, IPv4-mapped), each as a literal (IPv6 bracketed) and as a DNS answer, and a redirect to a private address, all refused. After deploy, from the container: a public URL answers, 192.168.0.1 is refused |
| V2. anonymous.4open.science repositories get checked | A rewrite unit test. On a dev `deep`, 2610.24495's card has `code_available: true` with its repository. The other 4 the same once remade in production |
| V3. The budget and byte cap hold | Unit tests: the 61st fetch throws; a 5 MB body is cut at 2 MB without being read whole |
| V4. Every attempt keeps its trace, none lost in the move | Pytest on a fresh schema, with a card whose trace came from a failed remake and a trace with no attempt (the migration fails). On a production-copy sandbox: 46 traces before and after, none left on a card. A failed or lost attempt keeps its own trace, or none, never a wiped one |
| V5. The agent can read what Deep did | `litwatch deep trace 2610.24495` output in P2's evidence: turns, cells, host calls with reasons and hops, timings |
| V6. Process notes reach the card and Needs you, folded and quiet | Deep tests with pi-ai's fake provider: model notes validated, service notes merged after. Screenshots at 390 and 1440 px, dark and light: the card's folded rung, the Deep notes group. Vitest and pytest: run status, `waiting` and the page count unchanged; Acknowledge; a remade card clears it |
| V7. Cards don't get worse | 3 previously fine papers remade on a dev `deep` at medium, compared with their production cards made at low, with each run's time and tokens |

## Scope

**In:**
- the open fetch with its network guard, and the anonymous.4open.science rewrite;
- Deep at medium effort;
- traces per attempt and the agent's CLI;
- process notes from the model and the service, on the card and on Needs you;
- the spec and glossary amendments;
- remaking the 5 cards.

**Out:**
- A trace UI for the owner: declined.
- A headless browser for pages built by JavaScript: not needed yet.
- Deep's model: the model-spend plan's.
- Process notes in Zotero and Notion.
- The Deep gold set: a known gap in verification.md.
