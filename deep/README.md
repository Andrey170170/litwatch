# litwatch-deep

The deep-card service: an agent with one JavaScript REPL tool reads a paper and writes the lab's
reading card. litwatch's `deep` integration submits papers to it (`POST /process`) and polls
for the result (`GET /jobs/<id>`).

A submission may carry an `Idempotency-Key` header (litwatch sends its attempt): a key the
service still has is answered `200` with that key's job instead of starting another, so a
submission sent again after a lost reply never runs twice; `GET /keys/<key>` finds that job
(404 when the service has none). A failed job (`status: "error"`)
says whose failure it was in `failure`: `service` when the model call failed (the sign-in, a
usage limit, the provider) or the job could not start, `paper` when no valid card was submitted
within the budget or the content filter stopped the model. Jobs and keys live in memory: a
restart forgets them.

A job's `trace` (in `GET /jobs/<id>`) is what the run did, each event with `t`, milliseconds since it
started: the model's `assistant` turns (text, reasoning summary, stop reason, usage), `cell`s (code,
output, error, time, and `timedOut` on the one that ran past its limit, which ends the session), `host`
calls (name, arguments cut to 200 characters, time, `ok`, the characters returned, and a failed call's
`error`, cut to 300), `submit`s, and the service's `system` nudges. A `fetch` call adds `fetch`: each
redirect it was answered with (`hops`, URL and status), then the `url` and `status` that answered and the
`bytes` of body read. The model is asked for detailed reasoning summaries. litwatch keeps every
attempt's trace and reads it with `litwatch deep trace`.

The card places the paper among litwatch's collection (`among_collection`): papers it agrees with,
contradicts, extends or supersedes. `submit` takes only ids a `store.search` in the same session
returned from the collection. `paper` is the archive's LaTeX source, or the PDF's text as markdown.

```sh
npm ci
npm test && npm run typecheck
node src/main.ts login    # sign in once; the auth file keeps the session
node src/main.ts serve
```

## Environment

| Variable | Default | |
| --- | --- | --- |
| `DEEP_PORT` | `8081` | Port to listen on |
| `DEEP_AUTH_FILE` | `/var/lib/litwatch-deep/auth.json` | Where `login` keeps the model session |
| `DEEP_MODEL`, `DEEP_EFFORT` | `gpt-6.1-sol`, `medium` | The agent's model and reasoning effort |
| `DEEP_MAX_CELLS` | `40` | REPL cells per paper |
| `DEEP_CELL_TIMEOUT_S` | `90` | One cell's time limit |
| `DEEP_WALL_CLOCK_MIN` | `30` | One paper's time limit; keep it below litwatch's `[integrations.deep] timeout_minutes` (45), past which litwatch ends a running job as stuck and may start a duplicate |
| `DEEP_MAX_OUTPUT_CHARS` | `8000` | Output kept per cell |
| `DEEP_MAX_FETCH_CHARS` | `200000` | Text kept per `fetch` |
| `DEEP_MAX_FETCH_MB` | `2` | MiB read per `fetch`, fractions allowed; the rest of the body is never read |
| `DEEP_MAX_FETCHES` | `60` | `fetch` calls per paper; the next one fails in the cell |
| `LITWATCH_URL` | `http://litwatch:8080` | litwatch v1, for the agent's `store.search` (`GET /api/papers`) |
| `LITWATCH_TOKEN` | | litwatch's read token (`LITWATCH_READ_TOKEN` there), which may only search and read papers |
| `S2_API_KEY` | | Semantic Scholar key; without one `s2.lookup` is rate-limited |
| `DEEP_CONTACT` | | An address the fetched sites can write to, sent in the User-Agent (arXiv asks for one) |

A secret may also be given as a file: `LITWATCH_TOKEN_FILE`, `S2_API_KEY_FILE`.

The agent's `fetch` reaches any public site and never a private one (litwatch decision 80). It is a GET with
the service's own user-agent, no cookies, and no headers the agent sets. Every address a name resolves to must
be public unicast, checked where the socket connects, and a literal address in the URL is checked up front, so
loopback, the LAN, link-local (cloud metadata) and unique-local addresses are refused, and so are the IPv6
transition ranges that embed an IPv4 address (NAT64, 6to4, Teredo). An IPv4-mapped IPv6 address
(`::ffff:a.b.c.d`) is judged as the IPv4 address it carries. Counts must be whole numbers; a size or time may be fractional
and is rounded down to bytes or milliseconds. Each must come to at least one and stay within what its consumer
holds, or the service refuses to start. Redirects are followed by hand, at most five, each hop checked again. Each site is paced at one
request a second at most (arXiv and Semantic Scholar slower). anonymous.4open.science links are read through
its JSON API, since its pages are built in the browser. `store.search` and `s2.lookup` don't go through this
guard: litwatch itself is on a private address.

`store.search` asked litwatch 0.x's `/papers/search` before v1; a 0.14 deployment keeps running
the image it was built with.
