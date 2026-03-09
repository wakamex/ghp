# ghp

Stateless GitHub activity summary. Compact, LLM-friendly output.

## Install

```bash
uv tool install ghp
# or
uvx ghp
```

## Usage

```bash
ghp                                   # open issues + PRs snapshot
ghp 1h                                # deltas since 1 hour ago
ghp 2026-03-07T14:00:00Z              # deltas since timestamp
ghp --json                            # machine-readable output
ghp --me @clod                        # highlight mentions
ghp --repo owner/name                 # explicit repo
# default delta cursor comes from .ghp-last-update-timestamp if present
```

## Behavior

- Cursor defaults to `.ghp-last-update-timestamp` in the current working directory when `--since` is omitted.
- Successful runs autosave the current timestamp back to `.ghp-last-update-timestamp`.
- `--since` accepts relative shorthands (`30m`, `2h`, `1d`, `1w`) and normalizes timestamps to canonical UTC.
- Snapshot mode returns open issues and open PRs.
- Delta mode returns issues, PRs, issue comments, PR review comments, and recent commits since the cutoff.
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
scripts/test-install.sh
```
