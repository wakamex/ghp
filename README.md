# gh-pulse

Stateless GitHub activity summary. Compact, LLM-friendly output.

## Install

```bash
uv pip install -e /code/gh-pulse --system
# or
uvx --from /code/gh-pulse gh-pulse
```

## Usage

```bash
gh-pulse                              # open issues + PRs snapshot
gh-pulse --since 1h                   # deltas since 1 hour ago
gh-pulse --since 2026-03-07T14:00:00Z # deltas since timestamp
gh-pulse --json                       # machine-readable output
gh-pulse --me @clod                   # highlight mentions
gh-pulse --repo owner/name            # explicit repo
```

## Behavior

- Stateless by design: callers own the cursor via `--since`.
- `--since` accepts relative shorthands (`30m`, `2h`, `1d`, `1w`) and normalizes timestamps to canonical UTC.
- Snapshot mode returns open issues and open PRs.
- Delta mode returns issues, PRs, issue comments, and PR review comments updated since the cutoff.
- API or auth failures exit non-zero instead of silently pretending there was no activity.
- `--json` prints a machine-readable payload; on failure it emits a JSON object with an `error` field and exits non-zero.

## Auth

Looks for tokens in this order:
1. `$GITHUB_PAT`
2. `$GITHUB_TOKEN`
3. `$GH_TOKEN`
4. `gh auth token` (gh CLI)

## Test

```bash
PYTHONPATH=src python -m unittest discover -s tests
```
