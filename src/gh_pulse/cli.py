"""Stateless GitHub activity summary for a repo.

Compact, LLM-friendly output. Caller owns the cursor via --since.

Usage:
    gh-pulse                          # snapshot: open issues + PRs
    gh-pulse --since 1h               # deltas since 1 hour ago
    gh-pulse --since 2026-03-07T14:00:00Z
    gh-pulse --json                   # machine-readable output
    gh-pulse --me @clod               # highlight mentions
    gh-pulse --repo owner/name        # explicit repo (else auto-detect)
"""

import argparse
import json as json_mod
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone


def _get_token():
    """Find a GitHub token from env or gh CLI."""
    for var in ("GITHUB_PAT", "GITHUB_TOKEN", "GH_TOKEN"):
        tok = os.environ.get(var)
        if tok:
            return tok
    # Try gh CLI auth token
    try:
        result = subprocess.run(
            ["gh", "auth", "token"], capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return None


def _detect_repo():
    """Detect owner/repo from git remote."""
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=5
        )
        url = result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    # ssh: git@github.com:owner/repo.git
    m = re.search(r"github\.com[:/](.+?)(?:\.git)?$", url)
    if m:
        return m.group(1)
    return None


def _api(path, token):
    """GET from GitHub REST API. Returns parsed JSON."""
    url = f"https://api.github.com/{path.lstrip('/')}"
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json_mod.loads(resp.read())
    except urllib.error.HTTPError as e:
        print(f"API error: {e.code} {e.reason} for {url}", file=sys.stderr)
        return []


def _resolve_since(raw):
    """Parse --since value into UTC ISO 8601 string."""
    # Already ISO 8601
    if re.match(r"\d{4}-\d{2}-\d{2}T", raw):
        return raw
    # Relative: 30m, 2h, 1d, 1w
    m = re.match(r"^(\d+)([mhdw])$", raw)
    if not m:
        print(f"Invalid --since: {raw} (use 30m, 2h, 1d, 1w, or ISO 8601)", file=sys.stderr)
        sys.exit(1)
    num, unit = int(m.group(1)), m.group(2)
    delta = {
        "m": timedelta(minutes=num),
        "h": timedelta(hours=num),
        "d": timedelta(days=num),
        "w": timedelta(weeks=num),
    }[unit]
    dt = datetime.now(timezone.utc) - delta
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _fmt_issue(i):
    labels = ",".join(l["name"] for l in i.get("labels", []))
    label_str = f" [{labels}]" if labels else ""
    comment_str = f" ({i['comments']} comments)" if i.get("comments") else ""
    return f'  #{i["number"]} [{i["state"]}] "{i["title"]}" @{i["user"]["login"]}{label_str}{comment_str}'


def _fmt_pr(p):
    draft = ",draft" if p.get("draft") else ""
    review_str = f" ({p['review_comments']} review comments)" if p.get("review_comments") else ""
    return (
        f'  #{p["number"]} [{p["state"]}{draft}] '
        f'"{p["title"]}" @{p["user"]["login"]} '
        f'{p["head"]["ref"]} -> {p["base"]["ref"]}{review_str}'
    )


def _fmt_comment(c):
    # Extract issue/PR number from URL
    num = c["issue_url"].rstrip("/").split("/")[-1]
    body = c["body"].replace("\n", " ")
    if len(body) > 120:
        body = body[:120] + "..."
    return f"  #{num} @{c['user']['login']} {c['created_at']}: {body}"


def main():
    parser = argparse.ArgumentParser(
        prog="gh-pulse",
        description="Stateless GitHub activity summary — compact, LLM-friendly output",
    )
    parser.add_argument("--since", metavar="TIME",
                        help="Show deltas since TIME (ISO 8601 or relative: 30m, 2h, 1d, 1w)")
    parser.add_argument("--json", action="store_true",
                        help="Output as JSON")
    parser.add_argument("--me", metavar="HANDLE",
                        help="Highlight mentions of this handle (e.g. @clod)")
    parser.add_argument("--repo", metavar="OWNER/REPO",
                        help="GitHub repo (default: auto-detect from git remote)")
    parser.add_argument("--limit", type=int, default=30,
                        help="Max items per category (default: 30)")
    args = parser.parse_args()

    token = _get_token()
    repo = args.repo or _detect_repo()
    if not repo:
        print("Error: could not detect repo. Use --repo owner/name or run from a git checkout.", file=sys.stderr)
        sys.exit(1)

    cutoff = _resolve_since(args.since) if args.since else None
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # --- Fetch ---

    if cutoff:
        # Delta mode: items updated since cutoff
        raw = _api(f"repos/{repo}/issues?state=all&since={cutoff}&per_page={args.limit}&sort=updated&direction=desc", token)
    else:
        # Snapshot mode: open items
        raw = _api(f"repos/{repo}/issues?state=open&per_page={args.limit}&sort=updated&direction=desc", token)

    issues = [i for i in raw if "pull_request" not in i]
    pr_numbers = [i["number"] for i in raw if "pull_request" in i]

    # Get richer PR data from pulls endpoint
    prs = []
    if cutoff:
        all_prs = _api(f"repos/{repo}/pulls?state=all&sort=updated&direction=desc&per_page={args.limit}", token)
        prs = [p for p in all_prs if p.get("updated_at", "") >= cutoff]
    else:
        prs = _api(f"repos/{repo}/pulls?state=open&sort=updated&direction=desc&per_page={args.limit}", token)

    # Comments (delta mode only)
    comments = []
    if cutoff:
        comments = _api(f"repos/{repo}/issues/comments?since={cutoff}&per_page={args.limit}&sort=updated&direction=desc", token)

    # --- JSON output ---

    if args.json:
        out = {
            "repo": repo,
            "timestamp": now,
            "since": cutoff,
            "issues": [
                {
                    "number": i["number"], "title": i["title"], "state": i["state"],
                    "user": i["user"]["login"],
                    "labels": [l["name"] for l in i.get("labels", [])],
                    "updated_at": i["updated_at"], "created_at": i["created_at"],
                    "comments": i.get("comments", 0),
                }
                for i in issues
            ],
            "pull_requests": [
                {
                    "number": p["number"], "title": p["title"], "state": p["state"],
                    "user": p["user"]["login"],
                    "base": p["base"]["ref"], "head": p["head"]["ref"],
                    "draft": p.get("draft", False),
                    "updated_at": p["updated_at"], "created_at": p["created_at"],
                    "review_comments": p.get("review_comments", 0),
                    "comments": p.get("comments", 0),
                }
                for p in prs
            ],
            "recent_comments": [
                {
                    "issue_number": c["issue_url"].rstrip("/").split("/")[-1],
                    "user": c["user"]["login"],
                    "created_at": c["created_at"],
                    "body": c["body"][:200] + "..." if len(c["body"]) > 200 else c["body"],
                }
                for c in comments
            ],
        }
        print(json_mod.dumps(out, indent=2))
        return

    # --- Compact text output ---

    print(f"# {repo} | {now}")
    if cutoff:
        print(f"# since {cutoff}")
    else:
        print("# open items snapshot")
    print()

    if issues:
        print(f"ISSUES ({len(issues)})")
        for i in issues:
            print(_fmt_issue(i))
        print()

    if prs:
        print(f"PRS ({len(prs)})")
        for p in prs:
            print(_fmt_pr(p))
        print()

    if cutoff and comments:
        print(f"COMMENTS ({len(comments)} new)")
        for c in comments:
            print(_fmt_comment(c))
        print()

    # Mentions
    if args.me and comments:
        handle = args.me.lstrip("@")
        hits = [c for c in comments if f"@{handle}" in c.get("body", "").lower() or f"@{handle}" in c.get("body", "")]
        if hits:
            print(f"MENTIONS (@{handle}: {len(hits)})")
            for c in hits:
                print(_fmt_comment(c))
            print()

    if not issues and not prs and not comments:
        if cutoff:
            print(f"No activity since {cutoff}.")
        else:
            print("No open issues or PRs.")
