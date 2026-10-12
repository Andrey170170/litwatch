# syntax=docker/dockerfile:1.7

FROM ghcr.io/astral-sh/uv:0.11.16@sha256:440fd6477af86a2f1b38080c539f1672cd22acb1b1a47e321dba5158ab08864d AS uv

# The web app, built first so the Python package ships it at src/litwatch/api/web/.
FROM node:24-slim AS web

WORKDIR /app/web
RUN corepack enable
COPY web/package.json web/pnpm-lock.yaml ./
RUN --mount=type=cache,target=/root/.local/share/pnpm/store \
    pnpm install --frozen-lockfile
COPY web/ ./
# vite.config.ts writes to ../src/litwatch/api/web, so this lands in /app/src/litwatch/api/web.
RUN pnpm build


FROM python:3.13.13-slim-trixie@sha256:aa938a849bcb82dce8f49480f056ab82bf5c1c3ebc294f0430f37b6820e7f286 AS builder

COPY --from=uv /uv /uvx /usr/local/bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

# Dependencies first so source edits do not invalidate the dependency layer.
COPY pyproject.toml uv.lock README.md ./
COPY vendor/ vendor/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

COPY . ./
COPY --from=web /app/src/litwatch/api/web src/litwatch/api/web
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev


FROM python:3.13.13-slim-trixie@sha256:aa938a849bcb82dce8f49480f056ab82bf5c1c3ebc294f0430f37b6820e7f286 AS runtime

ARG VERSION=0.0.0-dev
ARG REVISION=unknown
ARG SOURCE=https://github.com/Andrey170170/litwatch

LABEL org.opencontainers.image.title="litwatch" \
      org.opencontainers.image.description="Literature watch for CoT monitoring and faithfulness papers" \
      org.opencontainers.image.source="${SOURCE}" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${REVISION}"

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/var/lib/litwatch

WORKDIR /var/lib/litwatch

COPY --from=builder --chown=0:0 /app/.venv /app/.venv
COPY --from=builder --chown=0:0 /app/src /app/src

# Fixed 1000:1000 so bind-mounted secret files can be readable without root.
RUN groupadd --gid 1000 litwatch && \
    useradd --uid 1000 --gid 1000 --home-dir /var/lib/litwatch \
        --shell /usr/sbin/nologin litwatch && \
    install -d -o litwatch -g litwatch -m 0750 /var/lib/litwatch && \
    chmod -R a+rX /app && chmod -R a-w /app

USER 1000:1000

HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=5).status == 200 else 1)"]

ENTRYPOINT ["litwatch"]
CMD ["serve"]
