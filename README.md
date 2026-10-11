# ghp

Compact GitHub activity summary with repository-scoped checkpoints.

## Install

```bash
uv tool install ghp
# or
uvx ghp
```

## Dev

```bash
uv sync
uv run ghp --version
```

## Usage

```bash
ghp                                   # open issues + PRs snapshot
ghp upstream                          # use the GitHub repo from the upstream remote
ghp 1h                                # deltas since 1 hour ago
ghp upstream 1h                       # upstream deltas since 1 hour ago
ghp 2026-03-07T14:00:00Z              # deltas since timestamp
ghp --json                            # machine-readable output
ghp --me @clod                        # highlight mentions
ghp --repo owner/name                 # explicit repo
ghp inbox                             # unread notifications across all repositories
ghp inbox 1d                          # read and unread notifications since 1 day ago
ghp inbox --repo owner/name           # unread notifications for one repository
```

## Behavior

- Cursor defaults to the saved checkpoint for the current GitHub repository when `--since` is
  omitted.
- `upstream` selects the repository configured as the Git remote named `upstream`.
- Checkpoints are stored under `$XDG_STATE_HOME/ghp/checkpoints/`. When `XDG_STATE_HOME` is unset or
  invalid, ghp uses `~/.local/state/ghp/checkpoints/`.
- Each repository has its own atomically replaced checkpoint file, so `origin`, `upstream`, and
  `--repo` queries do not advance each other's checkpoints.
- Saved checkpoints include a one-second margin because GitHub's `since` filters are strict and
  timestamps have second precision. Boundary activity may appear again on the next run.
- Tool installs and upgrades leave checkpoint state in place.
- Repository-keyed `.ghp-last-update-timestamp` files are migrated to XDG state and removed after
  successful writes. Older plaintext files require an explicit window such as `ghp 1h` once because
  they do not identify a repository.
- `--since` accepts relative shorthands (`30m`, `2h`, `1d`, `1w`) and normalizes timestamps to
  canonical UTC.
- Snapshot mode returns open issues and open PRs.
- Delta mode returns issues, PRs, issue comments, PR review comments, and recent commits since the cutoff.
- `inbox` lists the authenticated account's notifications, grouped by repository and newest first. It ignores the current checkout and reads and writes no checkpoint: GitHub's unread state is the cursor, and ghp never marks a notification as read.
- `inbox` alone returns unread notifications. With a TIME it returns read and unread notifications updated since then, and marks the read ones `read`.
- Each inbox line gives the issue or pull request number (or short commit hash), the subject type, the reason GitHub sent it (such as `mention`, `review_requested`, or `author`), the update time, and the title.
- API or auth failures exit non-zero instead of silently pretending there was no activity.
- `--json` prints a machine-readable payload; on failure it emits a JSON object with an `error` field
  and exits non-zero.

## Auth

Looks for tokens in this order:

1. `$GITHUB_PAT`
2. `$GITHUB_TOKEN`
3. `$GH_TOKEN`
4. `gh auth token` (gh CLI)

`ghp inbox` always needs a token, and GitHub's notifications API accepts only a `gh` CLI token or a classic personal access token with the `notifications` or `repo` scope. Fine-grained personal access tokens are rejected.

## Test

```bash
uv run python -m unittest discover -s tests
scripts/test-install.sh
```
