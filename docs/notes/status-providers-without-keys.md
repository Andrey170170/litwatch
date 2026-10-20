# The status shows providers healthy without their keys

Seen while walking `docs/install.md` on a fresh stack (2026-10-20).

With `ANTHROPIC_API_KEY` and `OPENROUTER_API_KEY` empty, `/status` reported the provider components as healthy, while
`litwatch ping judge` failed with `CallError: provider: no credentials for anthropic`. A new install that forgot a key
learns it only when the first nightly fails.

The fix is for a provider that a configured role uses to read as down (with the reason "no credentials") when its key is
missing. Not reachable on the owner's instance, whose keys are set.
