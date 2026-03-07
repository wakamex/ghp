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
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

ISO_8601_UTC = "%Y-%m-%dT%H:%M:%SZ"
GITHUB_HANDLE_CHARS = "A-Za-z0-9-"


class ApiError(RuntimeError):
    """Raised when a GitHub API request fails."""


def _utc_now():
    return datetime.now(timezone.utc)


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
    labels = ",".join(label["name"] for label in issue.get("labels", []))
    label_str = f" [{labels}]" if labels else ""
    comment_str = (
        f" ({issue['comments']} comments)" if issue.get("comments") else ""
    )
    return (
        f'  #{issue["number"]} [{issue["state"]}] "{issue["title"]}" '
        f'@{issue["user"]["login"]}{label_str}{comment_str}'
    )


def _fmt_pr(pr):
    status = pr["state"]
    if pr.get("draft"):
        status = f"{status},draft"

    extras = []
    if isinstance(pr.get("comments"), int) and pr["comments"] > 0:
        extras.append(f"{pr['comments']} comments")
    if isinstance(pr.get("review_comments"), int) and pr["review_comments"] > 0:
        extras.append(f"{pr['review_comments']} review comments")
    extra_str = f" ({', '.join(extras)})" if extras else ""

    return (
        f'  #{pr["number"]} [{status}] "{pr["title"]}" @{pr["user"]["login"]} '
        f'{pr["head"]["ref"]} -> {pr["base"]["ref"]}{extra_str}'
    )


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
        f"  #{_comment_number(comment)} {kind} @{comment['user']['login']} "
        f"{_comment_timestamp(comment)}: {_trim_comment_body(comment.get('body'), 120)}"
    )


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
                indent=2,
            )
        )
        return
    print(f"Error: {message}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(
        prog="gh-pulse",
        description="Stateless GitHub activity summary — compact, LLM-friendly output",
    )
    parser.add_argument(
        "--since",
        metavar="TIME",
        help="Show deltas since TIME (ISO 8601 or relative: 30m, 2h, 1d, 1w)",
    )
    parser.add_argument("--json", action="store_true", help="Output as JSON")
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
        cutoff = _resolve_since(args.since) if args.since else None
    except ValueError as exc:
        _emit_error(str(exc), args.json, repo, now, cutoff)
        return 1

    token = _get_token()

    try:
        issues = _fetch_issues(repo, token, args.limit, cutoff)
        prs = _fetch_prs(repo, token, args.limit, cutoff)
        comments = _fetch_comments(repo, token, args.limit, cutoff)
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
        }
        print(json_mod.dumps(out, indent=2))
        return 0

    print(f"# {repo} | {now}")
    if cutoff:
        print(f"# since {cutoff}")
    else:
        print("# open items snapshot")
    print()

    if issues:
        print(f"ISSUES ({len(issues)})")
        for issue in issues:
            print(_fmt_issue(issue))
        print()

    if prs:
        print(f"PRS ({len(prs)})")
        for pr in prs:
            print(_fmt_pr(pr))
        print()

    if cutoff and comments:
        print(f"COMMENTS ({len(comments)} updated)")
        for comment in comments:
            print(_fmt_comment(comment))
        print()

    mention_re = _mention_pattern(args.me) if args.me else None
    if mention_re and comments:
        hits = [
            comment for comment in comments if mention_re.search(comment.get("body") or "")
        ]
        if hits:
            handle = args.me.lstrip("@")
            print(f"MENTIONS (@{handle}: {len(hits)})")
            for comment in hits:
                print(_fmt_comment(comment))
            print()

    if not issues and not prs and not comments:
        if cutoff:
            print(f"No activity since {cutoff}.")
        else:
            print("No open issues or PRs.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
