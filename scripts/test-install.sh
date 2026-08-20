#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
dist="$tmp/dist"

cd "$root"

export UV_CACHE_DIR="$tmp/cache-build"
export HOME="$tmp/home"
mkdir -p "$HOME"

uv --no-config lock --check
uv --no-config build --no-sources --out-dir "$dist"

UV_TOOL_DIR="$tmp/tools-dist" UV_CACHE_DIR="$tmp/cache-dist" \
  uv --no-config tool install --force --no-index --find-links "$dist" ghp
"$HOME/.local/bin/ghp" --version

UV_TOOL_DIR="$tmp/tools-editable" UV_CACHE_DIR="$tmp/cache-editable" \
  uv --no-config tool install --force -e .
"$HOME/.local/bin/ghp" --version
