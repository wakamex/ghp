import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from unittest import mock

from gh_pulse import cli


class ResolveSinceTests(unittest.TestCase):
    def test_resolve_since_normalizes_naive_iso_to_utc(self):
        self.assertEqual(
            cli._resolve_since("2026-03-07T14:00:00"),
            "2026-03-07T14:00:00Z",
        )

    def test_resolve_since_normalizes_offset_iso_to_utc(self):
        self.assertEqual(
            cli._resolve_since("2026-03-07T09:00:00-05:00"),
            "2026-03-07T14:00:00Z",
        )


class MentionTests(unittest.TestCase):
    def test_mention_pattern_is_case_insensitive_and_boundary_aware(self):
        pattern = cli._mention_pattern("@Clod")

        self.assertIsNotNone(pattern)
        self.assertTrue(pattern.search("ping @clod please"))
        self.assertFalse(pattern.search("ping @cloddy please"))


class FetchTests(unittest.TestCase):
    def test_fetch_issues_paginates_past_pull_requests(self):
        first_page = [
            {"number": number, "pull_request": {"url": f"https://example.test/{number}"}}
            for number in range(1, 30)
        ]
        first_page.append({"number": 30, "title": "issue-30"})
        second_page = [
            {"number": 31, "title": "issue-31"},
            {"number": 32, "title": "issue-32"},
        ]

        with mock.patch("gh_pulse.cli._api", side_effect=[first_page, second_page]):
            issues = cli._fetch_issues("owner/repo", "token", 3, None)

        self.assertEqual([30, 31, 32], [issue["number"] for issue in issues])

    def test_fetch_comments_merges_issue_and_review_comments(self):
        issue_comments = [
            {
                "id": 10,
                "comment_type": "issue",
                "issue_url": "https://api.github.com/repos/owner/repo/issues/7",
                "user": {"login": "issue-user"},
                "body": "issue comment",
                "created_at": "2026-03-07T16:00:00Z",
                "updated_at": "2026-03-07T16:00:00Z",
            }
        ]
        review_comments = [
            {
                "id": 20,
                "comment_type": "review",
                "pull_request_url": "https://api.github.com/repos/owner/repo/pulls/8",
                "user": {"login": "review-user"},
                "body": "review comment",
                "created_at": "2026-03-07T17:00:00Z",
                "updated_at": "2026-03-07T17:00:00Z",
            }
        ]

        with mock.patch(
            "gh_pulse.cli._fetch_comment_endpoint",
            side_effect=[issue_comments, review_comments],
        ):
            comments = cli._fetch_comments(
                "owner/repo", "token", 1, "2026-03-07T15:00:00Z"
            )

        self.assertEqual([20], [comment["id"] for comment in comments])
        self.assertEqual("review", comments[0]["comment_type"])
        self.assertEqual("8", cli._comment_number(comments[0]))


class MainTests(unittest.TestCase):
    def test_main_emits_json_error_and_nonzero_exit_on_api_failure(self):
        stdout = io.StringIO()

        with mock.patch.object(
            sys, "argv", ["gh-pulse", "--json", "--repo", "owner/repo"]
        ), mock.patch(
            "gh_pulse.cli._fetch_issues", side_effect=cli.ApiError("boom")
        ), redirect_stdout(stdout):
            code = cli.main()

        self.assertEqual(1, code)
        payload = json.loads(stdout.getvalue())
        self.assertEqual("owner/repo", payload["repo"])
        self.assertEqual("boom", payload["error"])


if __name__ == "__main__":
    unittest.main()
