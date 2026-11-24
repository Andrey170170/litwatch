#!/usr/bin/env bash
# Regenerate every file made from the contracts the deep service declares in TypeBox
# (docs/plans/a2-work-gate.md, D2): the Deep read's JSON Schema (deep/src/read.ts) and the web
# app's types from it; contracts/*.schema.json (deep/src/contracts.ts) and litwatch's models from
# them, one module per schema in src/litwatch/core/contracts/; and, since litwatch's API uses
# those models, its OpenAPI schema (web/openapi.json) and the web app's client from it. Run from
# anywhere in the checkout after changing either source; CI runs it and fails when a committed
# file differs. Needs deep's and web's dependencies installed (`npm ci` in deep/, `pnpm install`
# in web/) and the dev extra (`uv sync --extra dev`).
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$root"

npm run --silent read-schema --prefix deep
npm run --silent contracts --prefix deep
(cd web && pnpm --silent generate:read)

for schema in contracts/*.schema.json; do
  out="src/litwatch/core/contracts/$(basename "$schema" .schema.json).py"
  uv run --quiet datamodel-codegen --input "$schema" --input-file-type jsonschema --output "$out" \
    --output-model-type pydantic_v2.BaseModel --target-python-version 3.13 \
    --enum-field-as-literal all --use-annotated --field-constraints --use-standard-collections \
    --use-union-operator --use-type-alias --use-schema-description --use-field-description \
    --disable-timestamp --no-formatters
  # the schema's root, which only holds the definitions
  sed -i '/^type Model = Any$/d' "$out"
  uv run --quiet ruff check --fix --quiet "$out"
  uv run --quiet ruff format --quiet "$out"
  echo "wrote $out"
done

# `litwatch openapi` builds the app on no database. Every litwatch command loads ./.env, so it
# runs in an empty directory with an empty environment: nothing of this checkout's reaches it.
empty="$(mktemp -d)"
trap 'rm -rf "$empty"' EXIT
(cd "$empty" && env -i PATH="$PATH" HOME="$HOME" uv run --quiet --project "$root" \
  litwatch openapi "$root/web/openapi.json")
(cd web && pnpm --silent generate)
