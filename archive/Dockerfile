# syntax=docker/dockerfile:1.7

# The archive's image: one image for its API (`paper-archive serve`), its acquisition worker
# (`paper-archive-worker`) and its search indexer (`paper-archive-indexer`), as the stack's
# compose file runs them. Data lives in volumes: /srv/arxiv-work (the catalog, the search
# index), /srv/arxiv-work/pacing (the arXiv pacing state, shared with litwatch's intake) and
# /srv/arxiv-archive (the stored files).

FROM ghcr.io/astral-sh/uv:0.13.4@sha256:9f130421e930bfbdc563c00740466603f8723158e90297d07957c2294640262b AS uv

FROM python:3.13.16-slim-trixie@sha256:cf50a4f6ac4f026458b08d5222023d700ac806410dff5e9ca8728fa44f2b850f AS builder

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


FROM python:3.13.16-slim-trixie@sha256:cf50a4f6ac4f026458b08d5222023d700ac806410dff5e9ca8728fa44f2b850f AS runtime

ARG VERSION=0.0.0-dev
ARG REVISION=unknown
ARG SOURCE=https://github.com/Andrey170170/litwatch

LABEL org.opencontainers.image.title="litwatch-archive" \
      org.opencontainers.image.description="arxiv-archive: the paper store litwatch ships with" \
      org.opencontainers.image.source="${SOURCE}" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${REVISION}" \
      org.opencontainers.image.licenses="Apache-2.0"

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY --from=builder --chown=0:0 /app/.venv /app/.venv
COPY --chown=0:0 LICENSE /app/LICENSE

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
