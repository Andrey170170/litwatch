# Internal shared package

`arxiv_archive-0.2.0-py3-none-any.whl` contains the lightweight base package from
https://github.com/Andrey170170/arxiv-archive, runtime source at commit `989f975`.
The artifact hash is recorded in `uv.lock`. Litwatch imports only its stdlib pacing
module; do not install the `[server]` extra into this container.

The wheel is checked in because this is a private internal dependency and image
builds should not need GitHub credentials. Refresh instructions are in
`docs/ARCHIVE-STAGE2.md`. This is not a separate fork of the pacing implementation.
