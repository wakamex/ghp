# gh-pulse

Stateless GitHub activity summary. Compact, LLM-friendly output.

## Install

```
uv pip install -e /code/gh-pulse --system
# or
uvx --from /code/gh-pulse gh-pulse
```

## Usage

```
gh-pulse                              # open issues + PRs snapshot
gh-pulse --since 1h                   # deltas since 1 hour ago
gh-pulse --since 2026-03-07T14:00:00Z # deltas since timestamp
gh-pulse --json                       # machine-readable output
gh-pulse --me @clod                   # highlight mentions
gh-pulse --repo owner/name            # explicit repo
```

## Auth

Looks for tokens in this order:
1. `$GITHUB_PAT`
2. `$GITHUB_TOKEN`
3. `$GH_TOKEN`
4. `gh auth token` (gh CLI)
