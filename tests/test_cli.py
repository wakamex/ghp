import io
import json
import os
import tempfile
import sys
import unittest
from contextlib import redirect_stdout
from unittest import mock

from ghp import cli


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

        with mock.patch("ghp.cli._api", side_effect=[first_page, second_page]):
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
            "ghp.cli._fetch_comment_endpoint",
            side_effect=[issue_comments, review_comments],
        ):
            comments = cli._fetch_comments(
                "owner/repo", "token", 1, "2026-03-07T15:00:00Z"
            )

        self.assertEqual([20], [comment["id"] for comment in comments])
        self.assertEqual("review", comments[0]["comment_type"])
        self.assertEqual("8", cli._comment_number(comments[0]))

    def test_fetch_commits_returns_empty_without_cutoff(self):
        self.assertEqual([], cli._fetch_commits("owner/repo", "token", 5, None))

    def test_fetch_commits_requests_commits_since_cutoff(self):
        commits = [
            {
                "sha": "abcdef123456",
                "commit": {
                    "message": "Fix bug\n\nMore detail",
                    "author": {"name": "Mihai", "date": "2026-03-07T17:00:00Z"},
                },
                "author": {"login": "mihai"},
                "html_url": "https://github.com/owner/repo/commit/abcdef123456",
            }
        ]

        with mock.patch("ghp.cli._api", return_value=commits) as api_mock:
            result = cli._fetch_commits(
                "owner/repo", "token", 3, "2026-03-07T15:00:00Z"
            )

        self.assertEqual(["abcdef123456"], [commit["sha"] for commit in result])
        api_mock.assert_called_once()


class TimestampFileTests(unittest.TestCase):
    def test_load_last_update_timestamp_returns_none_when_missing(self):
        with tempfile.TemporaryDirectory() as tmpdir, mock.patch("os.getcwd", return_value=tmpdir):
            self.assertIsNone(cli._load_last_update_timestamp())

    def test_save_and_load_last_update_timestamp_round_trip(self):
        with tempfile.TemporaryDirectory() as tmpdir, mock.patch("os.getcwd", return_value=tmpdir):
            cli._save_last_update_timestamp("2026-03-09T12:00:00Z")
            self.assertEqual(
                "2026-03-09T12:00:00Z",
                cli._load_last_update_timestamp(),
            )


class MainTests(unittest.TestCase):
    def test_main_emits_version(self):
        stdout = io.StringIO()

        with mock.patch.object(
            sys, "argv", ["ghp", "--version"]
        ), mock.patch(
            "ghp.cli._pkg_version", return_value="0.1.0"
        ), redirect_stdout(stdout):
            with self.assertRaises(SystemExit) as exc:
                cli.main()

        self.assertEqual(0, exc.exception.code)
        self.assertEqual("ghp 0.1.0\n", stdout.getvalue())

    def test_main_emits_json_error_and_nonzero_exit_on_api_failure(self):
        stdout = io.StringIO()

        with mock.patch.object(
            sys, "argv", ["ghp", "--json", "--repo", "owner/repo"]
        ), mock.patch(
            "ghp.cli._fetch_issues", side_effect=cli.ApiError("boom")
        ), redirect_stdout(stdout):
            code = cli.main()

        self.assertEqual(1, code)
        payload = json.loads(stdout.getvalue())
        self.assertEqual("owner/repo", payload["repo"])
        self.assertEqual("boom", payload["error"])

    def test_main_uses_last_update_file_when_since_is_omitted(self):
        stdout = io.StringIO()
        with tempfile.TemporaryDirectory() as tmpdir:
            timestamp_path = os.path.join(tmpdir, cli.LAST_UPDATE_FILENAME)
            with open(timestamp_path, "w", encoding="utf-8") as fh:
                fh.write("2026-03-07T15:00:00Z\n")

            with mock.patch.object(
                sys, "argv", ["ghp", "--repo", "owner/repo"]
            ), mock.patch("os.getcwd", return_value=tmpdir), mock.patch(
                "ghp.cli._fetch_issues", return_value=[]
            ), mock.patch(
                "ghp.cli._fetch_prs", return_value=[]
            ), mock.patch(
                "ghp.cli._fetch_comments", return_value=[]
            ), mock.patch(
                "ghp.cli._fetch_commits", return_value=[]
            ), redirect_stdout(stdout):
                code = cli.main()

        self.assertEqual(0, code)
        self.assertIn("since=2026-03-07T15:00:00Z", stdout.getvalue())

    def test_main_autosaves_last_update_timestamp_on_success(self):
        stdout = io.StringIO()
        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch.object(
                sys, "argv", ["ghp", "--repo", "owner/repo"]
            ), mock.patch("os.getcwd", return_value=tmpdir), mock.patch(
                "ghp.cli._fetch_issues", return_value=[]
            ), mock.patch(
                "ghp.cli._fetch_prs", return_value=[]
            ), mock.patch(
                "ghp.cli._fetch_comments", return_value=[]
            ), mock.patch(
                "ghp.cli._fetch_commits", return_value=[]
            ), mock.patch(
                "ghp.cli._utc_now",
                return_value=cli._parse_iso8601("2026-03-09T12:34:56Z"),
            ), redirect_stdout(stdout):
                code = cli.main()

            self.assertEqual(0, code)
            with open(
                os.path.join(tmpdir, cli.LAST_UPDATE_FILENAME), "r", encoding="utf-8"
            ) as fh:
                self.assertEqual("2026-03-09T12:34:56Z", fh.read().strip())

    def test_main_includes_commits_in_json_output(self):
        stdout = io.StringIO()
        commits = [
            {
                "sha": "abcdef123456",
                "commit": {
                    "message": "Fix bug\n\nMore detail",
                    "author": {"name": "Mihai", "date": "2026-03-07T17:00:00Z"},
                },
                "author": {"login": "mihai"},
                "html_url": "https://github.com/owner/repo/commit/abcdef123456",
            }
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch.object(
                sys, "argv", ["ghp", "--json", "--repo", "owner/repo", "--since", "1h"]
            ), mock.patch("os.getcwd", return_value=tmpdir), mock.patch(
                "ghp.cli._fetch_issues", return_value=[]
            ), mock.patch(
                "ghp.cli._fetch_prs", return_value=[]
            ), mock.patch(
                "ghp.cli._fetch_comments", return_value=[]
            ), mock.patch(
                "ghp.cli._fetch_commits", return_value=commits
            ), redirect_stdout(stdout):
                code = cli.main()

        self.assertEqual(0, code)
        payload = json.loads(stdout.getvalue())
        self.assertEqual("abcdef123456", payload["commits"][0]["sha"])
        self.assertEqual("Fix bug", payload["commits"][0]["message"])

    def test_main_emits_compact_text_output(self):
        stdout = io.StringIO()
        issue = {
            "number": 7,
            "state": "open",
            "title": "trim output",
            "user": {"login": "alice"},
            "labels": [{"name": "bug"}, {"name": "p1"}],
            "comments": 2,
        }
        pr = {
            "number": 9,
            "state": "open",
            "title": "ship less text",
            "user": {"login": "bob"},
            "head": {"ref": "feat"},
            "base": {"ref": "main"},
            "comments": 1,
            "review_comments": 3,
            "draft": True,
        }
        comment = {
            "id": 20,
            "comment_type": "review",
            "pull_request_url": "https://api.github.com/repos/owner/repo/pulls/9",
            "user": {"login": "carol"},
            "body": "ping @clod with the latest diff",
            "created_at": "2026-03-07T17:00:00Z",
            "updated_at": "2026-03-07T17:00:00Z",
        }
        commits = [
            {
                "sha": "abcdef123456",
                "commit": {
                    "message": "tighten output\n\nmore detail",
                    "author": {"name": "Dave", "date": "2026-03-07T17:30:00Z"},
                },
                "author": {"login": "dave"},
            }
        ]

        with mock.patch.object(
            sys,
            "argv",
            ["ghp", "--repo", "owner/repo", "--since", "1h", "--me", "@clod"],
        ), mock.patch("ghp.cli._utc_now", return_value=cli._parse_iso8601("2026-03-07T18:00:00Z")), mock.patch(
            "ghp.cli._fetch_issues", return_value=[issue]
        ), mock.patch("ghp.cli._fetch_prs", return_value=[pr]), mock.patch(
            "ghp.cli._fetch_comments", return_value=[comment]
        ), mock.patch(
            "ghp.cli._fetch_commits", return_value=commits
        ), redirect_stdout(stdout):
            code = cli.main()

        self.assertEqual(0, code)
        self.assertEqual(
            "\n".join(
                [
                    "owner/repo 2026-03-07T18:00:00Z since=2026-03-07T17:00:00Z",
                    "issues 1",
                    "#7 open @alice trim output l:bug,p1 c:2",
                    "pr 1",
                    "#9 open,draft @bob feat->main ship less text c:1 rc:3",
                    "comments 1",
                    "#9 review @carol 2026-03-07T17:00:00Z: ping @clod with the latest diff",
                    "commits 1",
                    "abcdef1 @dave 2026-03-07T17:30:00Z tighten output",
                    "@clod 1",
                    "#9 review @carol 2026-03-07T17:00:00Z: ping @clod with the latest diff",
                    "",
                ]
            ),
            stdout.getvalue(),
        )

    def test_main_accepts_positional_since_shorthand(self):
        stdout = io.StringIO()

        with mock.patch.object(
            sys, "argv", ["ghp", "1h", "--repo", "owner/repo"]
        ), mock.patch(
            "ghp.cli._utc_now",
            return_value=cli._parse_iso8601("2026-03-07T18:00:00Z"),
        ), mock.patch(
            "ghp.cli._fetch_issues", return_value=[]
        ), mock.patch(
            "ghp.cli._fetch_prs", return_value=[]
        ), mock.patch(
            "ghp.cli._fetch_comments", return_value=[]
        ), mock.patch(
            "ghp.cli._fetch_commits", return_value=[]
        ), redirect_stdout(stdout):
            code = cli.main()

        self.assertEqual(0, code)
        self.assertTrue(
            stdout.getvalue().startswith(
                "owner/repo 2026-03-07T18:00:00Z since=2026-03-07T17:00:00Z"
            )
        )


if __name__ == "__main__":
    unittest.main()
