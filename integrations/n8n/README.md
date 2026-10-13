# n8n: the Notion export

`litwatch-notion.json` is an n8n workflow that keeps a Notion database of the papers litwatch
puts at `relevant` or above for a Watch topic. Notion is not a built-in integration: litwatch's
`webhook` integration POSTs events to the workflow, which reads the paper from litwatch's API
and creates or updates its Notion page. Only Watch topics are exported; Radar and Pulse picks
are not.

What it does with each event, before it answers: reads `GET /api/papers/<paper_id>`, keeps the Watch topics at
`relevant` or above, finds the page whose `arXiv` property is the paper's id, and updates it, or
creates it when the paper is in the library. A paper that leaves the library keeps its page with
`In library` unticked; nothing is deleted.

## Set up

1. **Notion.** Make a database with these properties: `Title` (title), `arXiv`, `Authors`,
   `Take`, `Venue` (text), `Topics` (multi-select), `Grade` (select), `Published` (date), `Link`
   (URL), `In library` (checkbox). Create a Notion integration, share the database with it, and
   note its token and the database id.
2. **n8n.** Import `litwatch-notion.json`. Create two *Header Auth* credentials:
   `litwatch token` (`Authorization` = `Bearer <LITWATCH_API_TOKEN>`) and
   `Notion integration token` (`Authorization` = `Bearer <the Notion token>`). In the
   *Settings* node, set `litwatch_url` and `notion_database_id`. In the *litwatch event* node,
   change the path to something unguessable; it is the only thing between the internet and the
   workflow unless you add the signature check below. Activate the workflow.
3. **litwatch.** Add a webhook section to `litwatch.toml` and restart `litwatch serve`:

   ```toml
   [integrations.notion]
   use = "webhook"
   url = "https://n8n.example.org/webhook/<the path you chose>"
   events = ["grade.changed", "take.written", "paper.saved", "paper.unsaved", "publication.found"]
   # secret_env = "LITWATCH_NOTION_HOOK_SECRET"   # to sign each request
   ```

   The integration starts at the outbox's end, so pages appear for changes from now on. To
   export the library as it already is, run the workflow once per paper by hand, or post
   synthetic events to it.

## Notes

- The workflow answers litwatch only when its last node has run, so a failure anywhere (litwatch's
  API, Notion) answers an error. litwatch's webhook integration then sends the event again after
  a backoff (30 seconds, doubling, at most an hour), and the owner can retry or skip it under
  Settings → Integrations. Later events wait behind a failing one.
- Events can repeat after a failure, and several arrive for one change (`grade.changed`,
  `paper.saved`, then `take.written`). Each run reads the paper as it is now and writes the same
  page, so repeats are harmless. A page is created only when no page carries the paper's arXiv
  id, and litwatch sends one event at a time and waits for the answer, so two runs never race to
  create the same page. Keep the `arXiv` property unique: the workflow updates the first match.
- With `secret_env` set, each request carries `X-Litwatch-Signature: sha256=<hex HMAC-SHA256 of
  the raw body>`. To check it, add a Code node after the webhook that recomputes it (n8n must
  allow the `crypto` module: `NODE_FUNCTION_ALLOW_BUILTIN=crypto`) and stops on a mismatch.
- The workflow was written for n8n 1.x (Webhook v2, HTTP Request v4.2, Code v2, If v2) and
  Notion API version 2022-06-28. After importing, open each node once to check that the
  credentials are attached.
- `docs/integrations.md` describes the events and the webhook's request in full.
