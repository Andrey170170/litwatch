# syntax=docker/dockerfile:1.7

# The archive's image: one image for its API (`paper-archive serve`), its acquisition worker
# (`paper-archive-worker`) and its search indexer (`paper-archive-indexer`), as the stack's
# compose file runs them. Data lives in volumes: /srv/arxiv-work (the catalog, the search
# index), /srv/arxiv-work/pacing (the arXiv pacing state, shared with litwatch's intake) and
# /srv/arxiv-archive (the stored files).

FROM ghcr.io/astral-sh/uv:0.11.16@sha256:440fd6477af86a2f1b38080c539f1672cd22acb1b1a47e321dba5158ab08864d AS uv

FROM python:3.13.13-slim-trixie@sha256:aa938a849bcb82dce8f49480f056ab82bf5c1c3ebc294f0430f37b6820e7f286 AS builder

COPY --from=uv /uv /usr/local/bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app
RUN uv venv /app/.venv
COPY requirements.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --python /app/.venv -r requirements.lock
COPY pyproject.toml README.md ./
COPY src/ src/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --python /app/.venv --no-deps .


FROM python:3.13.13-slim-trixie@sha256:aa938a849bcb82dce8f49480f056ab82bf5c1c3ebc294f0430f37b6820e7f286 AS runtime

ARG VERSION=0.0.0-dev
ARG REVISION=unknown
ARG SOURCE=https://github.com/Andrey170170/litwatch

LABEL org.opencontainers.image.title="litwatch-archive" \
      org.opencontainers.image.description="arxiv-archive: the paper store litwatch ships with" \
      org.opencontainers.image.source="${SOURCE}" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${REVISION}"

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY --from=builder --chown=0:0 /app/.venv /app/.venv

# Fixed 1000:1000, as litwatch's image.
RUN groupadd --gid 1000 archive && \
    useradd --uid 1000 --gid 1000 --no-create-home --shell /usr/sbin/nologin archive && \
    install -d -o archive -g archive -m 0750 /srv/arxiv-work /srv/arxiv-work/pacing \
        /srv/arxiv-archive && \
    chmod -R a+rX /app && chmod -R a-w /app

USER 1000:1000
WORKDIR /srv/arxiv-work
EXPOSE 8787

# The token comes from $PAPER_ARCHIVE_TOKEN, or a file given with --token-file.
CMD ["paper-archive", "--no-mount-guard", "serve", "--host", "0.0.0.0", "--port", "8787"]
