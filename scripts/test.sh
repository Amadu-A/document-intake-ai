#!/usr/bin/env bash
# scripts/test.sh
# Единая Linux-проверка; весь вывод виден, дополнительные реальные проверки включаются флагами.
set -Eeuo pipefail
cd "$(dirname "$0")/.."
command -v uv >/dev/null || { echo 'Установите uv по https://docs.astral.sh/uv/'; exit 1; }
uv sync --frozen
for argument in "$@"; do
  if [[ "$argument" == --browser ]]; then uv run --frozen playwright install --no-shell chromium; fi
done
uv run --frozen ruff check .
uv run --frozen ruff format --check .
git diff --check
uv run --frozen pytest --cov=app --cov-report=term-missing "$@"
