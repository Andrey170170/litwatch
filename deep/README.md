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
| `DEEP_MODEL`, `DEEP_EFFORT` | `gpt-6.1-sol`, `low` | The agent's model and reasoning effort |
| `DEEP_MAX_CELLS` | `40` | REPL cells per paper |
| `DEEP_CELL_TIMEOUT_S` | `90` | One cell's time limit |
| `DEEP_WALL_CLOCK_MIN` | `30` | One paper's time limit |
| `DEEP_MAX_OUTPUT_CHARS` | `8000` | Output kept per cell |
| `DEEP_MAX_FETCH_CHARS` | `200000` | Text kept per `fetch` |
| `LITWATCH_URL` | `http://litwatch:8080` | litwatch v1, for the agent's `store.search` (`GET /api/papers`) |
| `LITWATCH_TOKEN` | | litwatch's read token (`LITWATCH_READ_TOKEN` there), which may only search and read papers |
| `S2_API_KEY` | | Semantic Scholar key; without one `s2.lookup` is rate-limited |

A secret may also be given as a file: `LITWATCH_TOKEN_FILE`, `S2_API_KEY_FILE`.

`store.search` asked litwatch 0.x's `/papers/search` before v1; a 0.14 deployment keeps running
the image it was built with.
