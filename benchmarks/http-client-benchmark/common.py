import json
import os
import subprocess
import sys
import time

API_BASE = "https://api.github.com"
LIMIT = 30


def parse_args(prototype):
    if len(sys.argv) != 3 or "/" not in sys.argv[1]:
        raise SystemExit(f"usage: {prototype} OWNER/REPO SINCE")
    return sys.argv[1:]


def github_token():
    for name in ("GITHUB_PAT", "GITHUB_TOKEN", "GH_TOKEN"):
        if token := os.environ.get(name):
            return token
    try:
        result = subprocess.run(
            ["gh", "auth", "token"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def github_headers(prototype):
    headers = {
        "User-Agent": f"ghp-{prototype}/0.1",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token := github_token():
        headers["Authorization"] = f"Bearer {token}"
    return headers


def request_specs(repo, since):
    return [
        (
            f"repos/{repo}/issues",
            {
                "state": "all",
                "sort": "updated",
                "direction": "desc",
                "since": since,
                "per_page": 60,
                "page": 1,
            },
        ),
        (
            f"repos/{repo}/pulls",
            {
                "state": "all",
                "sort": "updated",
                "direction": "desc",
                "per_page": LIMIT,
                "page": 1,
            },
        ),
        *[
            (
                f"repos/{repo}/{path}",
                {
                    "since": since,
                    "sort": "updated",
                    "direction": "desc",
                    "per_page": LIMIT,
                    "page": 1,
                },
            )
            for path in ("issues/comments", "pulls/comments")
        ],
        (
            f"repos/{repo}/commits",
            {"since": since, "per_page": LIMIT, "page": 1},
        ),
    ]


def emit_result(payloads, since, started):
    issues, prs, issue_comments, review_comments, commits = payloads
    counts = {
        "issues": min(sum("pull_request" not in issue for issue in issues), LIMIT),
        "pull_requests": min(
            sum(pr.get("updated_at", "") >= since for pr in prs), LIMIT
        ),
        "recent_comments": min(len(issue_comments) + len(review_comments), LIMIT),
        "commits": min(len(commits), LIMIT),
    }
    print(
        json.dumps(
            {
                "counts": counts,
                "fetch_ms": (time.perf_counter() - started) * 1000,
            },
            separators=(",", ":"),
        )
    )
