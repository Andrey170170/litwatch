# syntax=docker/dockerfile:1.7

FROM ghcr.io/astral-sh/uv:0.13.4@sha256:9f130421e930bfbdc563c00740466603f8723158e90297d07957c2294640262b AS uv

# The web app, built first so the Python package ships it at src/litwatch/api/web/.
FROM node:24.22.1-trixie-slim@sha256:43b9ba1142b8dcf33fa8f922ca1be85714adc444e0586c7cf7726daaeabc1bea AS web

WORKDIR /app/web
RUN corepack enable
COPY web/package.json web/pnpm-lock.yaml ./
RUN --mount=type=cache,target=/root/.local/share/pnpm/store \
    pnpm install --frozen-lockfile
COPY web/ ./
# vite.config.ts writes to ../src/litwatch/api/web, so this lands in /app/src/litwatch/api/web.
RUN pnpm build


FROM python:3.13.16-slim-trixie@sha256:cf50a4f6ac4f026458b08d5222023d700ac806410dff5e9ca8728fa44f2b850f AS builder

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


FROM python:3.13.16-slim-trixie@sha256:cf50a4f6ac4f026458b08d5222023d700ac806410dff5e9ca8728fa44f2b850f AS runtime

ARG VERSION=0.0.0-dev
ARG REVISION=unknown
ARG SOURCE=https://github.com/Andrey170170/litwatch

LABEL org.opencontainers.image.title="litwatch" \
      org.opencontainers.image.description="Literature watch for CoT monitoring and faithfulness papers" \
      org.opencontainers.image.source="${SOURCE}" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${REVISION}" \
      org.opencontainers.image.licenses="Apache-2.0"

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/var/lib/litwatch \
    LITWATCH_LOG_FORMAT=json

WORKDIR /var/lib/litwatch

COPY --from=builder --chown=0:0 /app/.venv /app/.venv
COPY --from=builder --chown=0:0 /app/src /app/src
# The evaluation labels, which a replay run from Overview reads (`quality.replay.LABELS`).
COPY --chown=0:0 evals/labels /app/evals/labels
COPY --chown=0:0 LICENSE /app/LICENSE

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
