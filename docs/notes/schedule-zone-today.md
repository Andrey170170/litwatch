# Today is the browser's, the run is the schedule's

Deferred from PR #85's last review round (Codex, 2026-10-18).

The web app takes "today" from the browser's clock (`today()` in `web/src/routes/digest.tsx`): the Digest tab opens
on it and the Day's › always stops on it. The Day's `run` (`today_run` in `api/digests.py`) is attached only to
today in the schedule's zone. When the two zones are on different dates (the owner travelling west of the server, or
the browser's clock set elsewhere), the tab opens on a day that has no `run` panel, and › stops there rather than
on the schedule's today.

The fix is for the app to take today from the server (the summary could carry the schedule's date), everywhere it
uses `today()`: the Day's default and stop, Week's "last week", History's pin and calendar outline. Not reachable
while the owner's browser and the server share America/Denver, which is the normal case.
