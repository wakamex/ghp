#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

cd "$root"

export UV_CACHE_DIR="$tmp/cache-build"
export HOME="$tmp/home"
mkdir -p "$HOME"

uv run --with build python -m build

UV_TOOL_DIR="$tmp/tools-dist" UV_CACHE_DIR="$tmp/cache-dist" \
  uv tool install --force --no-index --find-links dist ghp
"$HOME/.local/bin/ghp" --version

UV_TOOL_DIR="$tmp/tools-editable" UV_CACHE_DIR="$tmp/cache-editable" \
  uv tool install --force -e .
"$HOME/.local/bin/ghp" --version
