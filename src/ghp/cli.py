"""Stateless GitHub activity summary for a repo.

Compact, LLM-friendly output. Caller owns the cursor via --since.

Usage:
    ghp                               # snapshot: open issues + PRs
    ghp 1h                            # deltas since 1 hour ago
    ghp 2026-03-07T14:00:00Z
    ghp --json                        # machine-readable output
    ghp --me @clod                    # highlight mentions
    ghp --repo owner/name             # explicit repo (else auto-detect)
"""

import argparse
import json as json_mod
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from importlib.metadata import PackageNotFoundError, version

ISO_8601_UTC = "%Y-%m-%dT%H:%M:%SZ"
GITHUB_HANDLE_CHARS = "A-Za-z0-9-"
LAST_UPDATE_FILENAME = ".ghp-last-update-timestamp"


class ApiError(RuntimeError):
    """Raised when a GitHub API request fails."""


def _utc_now():
    return datetime.now(timezone.utc)


def _pkg_version():
    try:
        return version("ghp")
    except PackageNotFoundError:
        return "0.0.0"


def _format_utc(dt):
    return dt.astimezone(timezone.utc).strftime(ISO_8601_UTC)


def _parse_iso8601(raw):
    try:
        normalized = raw.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"invalid ISO 8601 timestamp: {raw}") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _get_token():
    """Find a GitHub token from env or gh CLI."""
    for var in ("GITHUB_PAT", "GITHUB_TOKEN", "GH_TOKEN"):
        tok = os.environ.get(var)
        if tok:
            return tok
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
            capture_output=True,
            text=True,
            timeout=5,
        )
        url = result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    match = re.search(r"github\.com[:/](.+?)(?:\.git)?$", url)
    if match:
        return match.group(1)
    return None


def _api(path, token, params=None):
    """GET JSON from GitHub REST API."""
    url = f"https://api.github.com/{path.lstrip('/')}"
    if params:
        url = f"{url}?{urllib.parse.urlencode(params, doseq=True)}"

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
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            payload = json_mod.loads(exc.read().decode())
            detail = payload.get("message", "")
        except Exception:
            detail = ""
        suffix = f" ({detail})" if detail else ""
        raise ApiError(f"API error: {exc.code} {exc.reason} for {url}{suffix}") from exc
    except urllib.error.URLError as exc:
        raise ApiError(f"API error: {exc.reason} for {url}") from exc


def _resolve_since(raw):
    """Parse --since value into a canonical UTC ISO 8601 string."""
    if re.match(r"^\d{4}-\d{2}-\d{2}T", raw):
        return _format_utc(_parse_iso8601(raw))

    match = re.match(r"^(\d+)([mhdw])$", raw)
    if not match:
        raise ValueError(
            f"invalid --since: {raw} (use 30m, 2h, 1d, 1w, or ISO 8601)"
        )

    num, unit = int(match.group(1)), match.group(2)
    delta = {
        "m": timedelta(minutes=num),
        "h": timedelta(hours=num),
        "d": timedelta(days=num),
        "w": timedelta(weeks=num),
    }[unit]
    return _format_utc(_utc_now() - delta)


def _fmt_issue(issue):
    parts = [
        f'#{issue["number"]}',
        issue["state"],
        f'@{issue["user"]["login"]}',
        issue["title"],
    ]
    labels = ",".join(label["name"] for label in issue.get("labels", []))
    if labels:
        parts.append(f"l:{labels}")
    if issue.get("comments"):
        parts.append(f'c:{issue["comments"]}')
    return " ".join(parts)


def _fmt_pr(pr):
    status = pr["state"]
    if pr.get("draft"):
        status = f"{status},draft"

    parts = [
        f'#{pr["number"]}',
        status,
        f'@{pr["user"]["login"]}',
        f'{pr["head"]["ref"]}->{pr["base"]["ref"]}',
        pr["title"],
    ]
    if isinstance(pr.get("comments"), int) and pr["comments"] > 0:
        parts.append(f'c:{pr["comments"]}')
    if isinstance(pr.get("review_comments"), int) and pr["review_comments"] > 0:
        parts.append(f'rc:{pr["review_comments"]}')
    return " ".join(parts)


def _comment_number(comment):
    url = comment.get("pull_request_url") or comment.get("issue_url") or ""
    return url.rstrip("/").split("/")[-1] if url else "?"


def _comment_timestamp(comment):
    return comment.get("updated_at") or comment.get("created_at") or ""


def _trim_comment_body(body, limit):
    flattened = re.sub(r"\s+", " ", (body or "").replace("\r", " ")).strip()
    if not flattened:
        return "[no body]"
    if len(flattened) > limit:
        return flattened[:limit] + "..."
    return flattened


def _fmt_comment(comment):
    kind = "review" if comment.get("comment_type") == "review" else "comment"
    return (
        f"#{_comment_number(comment)} {kind} @{comment['user']['login']} "
        f"{_comment_timestamp(comment)}: {_trim_comment_body(comment.get('body'), 120)}"
    )


def _print_section(name, items, formatter):
    if not items:
        return
    print(f"{name} {len(items)}")
    for item in items:
        print(formatter(item))


def _fetch_issues(repo, token, limit, cutoff):
    params = {
        "state": "all" if cutoff else "open",
        "sort": "updated",
        "direction": "desc",
    }
    if cutoff:
        params["since"] = cutoff

    per_page = min(max(limit * 2, 30), 100)
    issues = []
    page = 1

    while len(issues) < limit:
        batch = _api(
            f"repos/{repo}/issues",
            token,
            {**params, "per_page": per_page, "page": page},
        )
        if not isinstance(batch, list):
            raise ApiError("Unexpected issues payload from GitHub API")
        if not batch:
            break

        for item in batch:
            if "pull_request" in item:
                continue
            issues.append(item)
            if len(issues) >= limit:
                break

        if len(batch) < per_page:
            break
        page += 1

    return issues


def _fetch_prs(repo, token, limit, cutoff):
    params = {
        "state": "all" if cutoff else "open",
        "sort": "updated",
        "direction": "desc",
    }
    per_page = min(max(limit, 30), 100)
    cutoff_dt = _parse_iso8601(cutoff) if cutoff else None
    prs = []
    page = 1

    while len(prs) < limit:
        batch = _api(
            f"repos/{repo}/pulls",
            token,
            {**params, "per_page": per_page, "page": page},
        )
        if not isinstance(batch, list):
            raise ApiError("Unexpected pull request payload from GitHub API")
        if not batch:
            break

        for pr in batch:
            if cutoff_dt and _parse_iso8601(pr["updated_at"]) < cutoff_dt:
                return prs
            prs.append(pr)
            if len(prs) >= limit:
                return prs

        if len(batch) < per_page:
            break
        page += 1

    return prs


def _fetch_comment_endpoint(repo, token, path, comment_type, limit, cutoff):
    per_page = min(max(limit, 30), 100)
    comments = []
    page = 1

    while len(comments) < limit:
        batch = _api(
            f"repos/{repo}/{path}",
            token,
            {
                "since": cutoff,
                "sort": "updated",
                "direction": "desc",
                "per_page": per_page,
                "page": page,
            },
        )
        if not isinstance(batch, list):
            raise ApiError("Unexpected comments payload from GitHub API")
        if not batch:
            break

        for comment in batch:
            typed_comment = dict(comment)
            typed_comment["comment_type"] = comment_type
            comments.append(typed_comment)
            if len(comments) >= limit:
                break

        if len(batch) < per_page:
            break
        page += 1

    return comments


def _comment_sort_key(comment):
    updated_at = _parse_iso8601(_comment_timestamp(comment))
    created_at = _parse_iso8601(comment.get("created_at") or _comment_timestamp(comment))
    return (updated_at, created_at, comment.get("id", 0))


def _merge_comments(comment_groups, limit):
    merged = []
    for group in comment_groups:
        merged.extend(group)
    merged.sort(key=_comment_sort_key, reverse=True)
    return merged[:limit]


def _fetch_comments(repo, token, limit, cutoff):
    if not cutoff:
        return []

    issue_comments = _fetch_comment_endpoint(
        repo, token, "issues/comments", "issue", limit, cutoff
    )
    review_comments = _fetch_comment_endpoint(
        repo, token, "pulls/comments", "review", limit, cutoff
    )
    return _merge_comments([issue_comments, review_comments], limit)


def _fetch_commits(repo, token, limit, cutoff):
    if not cutoff:
        return []

    per_page = min(max(limit, 30), 100)
    commits = []
    page = 1

    while len(commits) < limit:
        batch = _api(
            f"repos/{repo}/commits",
            token,
            {
                "since": cutoff,
                "per_page": per_page,
                "page": page,
            },
        )
        if not isinstance(batch, list):
            raise ApiError("Unexpected commits payload from GitHub API")
        if not batch:
            break

        commits.extend(batch[: max(limit - len(commits), 0)])

        if len(batch) < per_page:
            break
        page += 1

    return commits[:limit]


def _mention_pattern(handle):
    normalized = handle.lstrip("@").strip()
    if not normalized:
        return None
    return re.compile(
        rf"(?<![{GITHUB_HANDLE_CHARS}])@{re.escape(normalized)}(?![{GITHUB_HANDLE_CHARS}])",
        re.IGNORECASE,
    )


def _emit_error(message, json_output, repo, timestamp, cutoff):
    if json_output:
        print(
            json_mod.dumps(
                {
                    "repo": repo,
                    "timestamp": timestamp,
                    "since": cutoff,
                    "error": message,
                },
                separators=(",", ":"),
            )
        )
        return
    print(f"Error: {message}", file=sys.stderr)


def _timestamp_file_path():
    return os.path.join(os.getcwd(), LAST_UPDATE_FILENAME)


def _load_last_update_timestamp():
    path = _timestamp_file_path()
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as fh:
        raw = fh.read().strip()
    if not raw:
        return None
    return _resolve_since(raw)


def _save_last_update_timestamp(timestamp):
    path = _timestamp_file_path()
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(timestamp + "\n")


def _fmt_commit(commit):
    sha = (commit.get("sha") or "")[:7] or "?"
    message = (commit.get("commit") or {}).get("message") or ""
    summary = message.splitlines()[0].strip() if message else "[no message]"
    author = (
        ((commit.get("author") or {}).get("login"))
        or ((commit.get("commit") or {}).get("author") or {}).get("name")
        or "unknown"
    )
    date = ((commit.get("commit") or {}).get("author") or {}).get("date") or ""
    return f"{sha} @{author} {date} {summary}"


def main():
    parser = argparse.ArgumentParser(
        prog="ghp",
        description="Stateless GitHub activity summary — compact, LLM-friendly output",
    )
    parser.add_argument(
        "since_pos",
        nargs="?",
        metavar="TIME",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--since",
        metavar="TIME",
        help="Show deltas since TIME (ISO 8601 or relative: 30m, 2h, 1d, 1w)",
    )
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {_pkg_version()}"
    )
    parser.add_argument(
        "--me", metavar="HANDLE", help="Highlight mentions of this handle (e.g. @clod)"
    )
    parser.add_argument(
        "--repo",
        metavar="OWNER/REPO",
        help="GitHub repo (default: auto-detect from git remote)",
    )
    parser.add_argument(
        "--limit", type=int, default=30, help="Max items per category (default: 30)"
    )
    args = parser.parse_args()
    if args.since and args.since_pos:
        parser.error("use TIME or --since, not both")

    now = _format_utc(_utc_now())
    repo = args.repo or _detect_repo()
    cutoff = None

    if args.limit < 1:
        _emit_error("--limit must be >= 1", args.json, repo, now, cutoff)
        return 1

    if not repo:
        _emit_error(
            "could not detect repo. Use --repo owner/name or run from a git checkout.",
            args.json,
            repo,
            now,
            cutoff,
        )
        return 1

    try:
        raw_since = args.since or args.since_pos
        cutoff = _resolve_since(raw_since) if raw_since else _load_last_update_timestamp()
    except ValueError as exc:
        _emit_error(str(exc), args.json, repo, now, cutoff)
        return 1

    token = _get_token()

    try:
        issues = _fetch_issues(repo, token, args.limit, cutoff)
        prs = _fetch_prs(repo, token, args.limit, cutoff)
        comments = _fetch_comments(repo, token, args.limit, cutoff)
        commits = _fetch_commits(repo, token, args.limit, cutoff)
    except ApiError as exc:
        _emit_error(str(exc), args.json, repo, now, cutoff)
        return 1

    if args.json:
        out = {
            "repo": repo,
            "timestamp": now,
            "since": cutoff,
            "issues": [
                {
                    "number": issue["number"],
                    "title": issue["title"],
                    "state": issue["state"],
                    "user": issue["user"]["login"],
                    "labels": [label["name"] for label in issue.get("labels", [])],
                    "updated_at": issue["updated_at"],
                    "created_at": issue["created_at"],
                    "comments": issue.get("comments", 0),
                }
                for issue in issues
            ],
            "pull_requests": [
                {
                    "number": pr["number"],
                    "title": pr["title"],
                    "state": pr["state"],
                    "user": pr["user"]["login"],
                    "base": pr["base"]["ref"],
                    "head": pr["head"]["ref"],
                    "draft": pr.get("draft", False),
                    "updated_at": pr["updated_at"],
                    "created_at": pr["created_at"],
                    "review_comments": pr.get("review_comments"),
                    "comments": pr.get("comments"),
                }
                for pr in prs
            ],
            "recent_comments": [
                {
                    "number": _comment_number(comment),
                    "comment_type": comment.get("comment_type", "issue"),
                    "user": comment["user"]["login"],
                    "created_at": comment.get("created_at"),
                    "updated_at": comment.get("updated_at"),
                    "body": _trim_comment_body(comment.get("body"), 200),
                }
                for comment in comments
            ],
            "commits": [
                {
                    "sha": commit.get("sha"),
                    "message": ((commit.get("commit") or {}).get("message") or "").splitlines()[0],
                    "author": (
                        ((commit.get("author") or {}).get("login"))
                        or ((commit.get("commit") or {}).get("author") or {}).get("name")
                    ),
                    "date": ((commit.get("commit") or {}).get("author") or {}).get("date"),
                    "url": commit.get("html_url"),
                }
                for commit in commits
            ],
        }
        print(json_mod.dumps(out, separators=(",", ":")))
        _save_last_update_timestamp(now)
        return 0

    head = f"{repo} {now}"
    if cutoff:
        head = f"{head} since={cutoff}"
    print(head)

    _print_section("iss", issues, _fmt_issue)
    _print_section("pr", prs, _fmt_pr)
    if cutoff:
        _print_section("com", comments, _fmt_comment)
        _print_section("git", commits, _fmt_commit)

    mention_re = _mention_pattern(args.me) if args.me else None
    if mention_re and comments:
        hits = [
            comment for comment in comments if mention_re.search(comment.get("body") or "")
        ]
        if hits:
            handle = args.me.lstrip("@")
            print(f"@{handle} {len(hits)}")
            for comment in hits:
                print(_fmt_comment(comment))

    if not issues and not prs and not comments and not commits:
        print("none")

    _save_last_update_timestamp(now)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
