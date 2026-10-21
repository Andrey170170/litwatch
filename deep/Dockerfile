# syntax=docker/dockerfile:1.7

FROM node:24.22.1-bookworm-slim@sha256:55fb5f5a527ea2c5c4a95f963e48ef90aa38c7b346054fdc8ee9957a2b55d205 AS deps

WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --omit=dev --no-audit --no-fund


FROM node:24.22.1-bookworm-slim@sha256:55fb5f5a527ea2c5c4a95f963e48ef90aa38c7b346054fdc8ee9957a2b55d205 AS runtime

ARG VERSION=0.0.0-dev
ARG REVISION=unknown
ARG SOURCE=https://github.com/Andrey170170/litwatch

LABEL org.opencontainers.image.title="litwatch-deep" \
      org.opencontainers.image.description="Deep processor for litwatch: a Pi agent with one JavaScript REPL tool" \
      org.opencontainers.image.source="${SOURCE}" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${REVISION}" \
      org.opencontainers.image.licenses="Apache-2.0"

ENV NODE_ENV=production \
    HOME=/var/lib/litwatch-deep \
    DEEP_AUTH_FILE=/var/lib/litwatch-deep/auth.json

WORKDIR /app
COPY --from=deps --chown=0:0 /app/node_modules ./node_modules
COPY --chown=0:0 package.json ./
COPY --chown=0:0 src ./src
COPY --chown=0:0 LICENSE ./

# The image's `node` user is already 1000:1000, which is what the host state directory expects.
RUN install -d -o node -g node -m 0750 /var/lib/litwatch-deep && \
    chmod -R a+rX /app && chmod -R a-w /app

USER 1000:1000
EXPOSE 8081

HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD ["node", "-e", "fetch('http://127.0.0.1:8081/healthz').then(r => process.exit(r.ok ? 0 : 1), () => process.exit(1))"]

ENTRYPOINT ["node", "src/main.ts"]
CMD ["serve"]
