# What a Deep paper leaves running after its session ends

Deferred from PR #111's last local review round (GPT-6.1-Sol, 2026-10-20), with the owner's agreement.

When a paper's session ends (it finished, or its wall clock ran out), `agent.ts` aborts the session's signal.
`fetch`, `store.search` and `s2.lookup` requests under way are aborted, queued calls never start, and a retry's
backoff stops. Two smaller things still outlive the session. Neither reaches anything the guard refuses, and
neither changes what the paper saw:

- A connection still being opened. undici's shared `Agent` has no session lifetime, so a TLS handshake or a DNS
  answer that comes after the abort can still finish opening its socket, which undici's connect timeout then closes.
- A pace cooldown that was already set. A lane's `setTimeout(release, wait)` (at most 3 s, for arXiv) runs to
  its end, and cancelled queued calls set their own.

The fixes tried were a pool of guarded sockets per paper, destroyed with its session, and cooldowns slept on
the session's signal (`timers/promises`). Under heavy parallel test load, the new wall-clock test in
`tests/agent.test.ts` then sometimes crashed the test process at exit, with isolated-vm's native assertion
`IsolateEnvironment::GetCurrent(): environment != nullptr`. It crashed in about 2–10% of runs, never in a process
that stayed alive, and never at the committed head. The cause wasn't found, so the attempt was dropped.

Pick this up again if leftover sockets or timers ever show in the running service. Before shipping, rerun the
load loop: 8 parallel `node --test --test-name-pattern="wall clock" tests/agent.test.ts`, several runs each.
