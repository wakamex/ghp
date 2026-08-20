import io
import json
import os
import tempfile
import sys
import threading
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

    def test_checkpoint_precedes_second_precision_query_start(self):
        self.assertEqual(
            "2026-03-07T13:59:59Z",
            cli._checkpoint_timestamp("2026-03-07T14:00:00Z"),
        )


class DetectRepoTests(unittest.TestCase):
    def test_detect_repo_reads_named_remote(self):
        result = mock.Mock(stdout="git@github.com:source/project.git\n")

        with mock.patch("ghp.cli.subprocess.run", return_value=result) as run_mock:
            repo = cli._detect_repo("upstream")

        self.assertEqual("source/project", repo)
        run_mock.assert_called_once_with(
            ["git", "remote", "get-url", "upstream"],
            capture_output=True,
            text=True,
            timeout=5,
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

    def test_fetch_activity_runs_delta_endpoints_concurrently(self):
        barrier = threading.Barrier(5)

        def finish_together(result):
            barrier.wait(timeout=2)
            return result

        issue = {"number": 1}
        pr = {"number": 2}
        issue_comment = {
            "id": 3,
            "created_at": "2026-03-07T16:00:00Z",
            "updated_at": "2026-03-07T16:00:00Z",
        }
        review_comment = {
            "id": 4,
            "created_at": "2026-03-07T17:00:00Z",
            "updated_at": "2026-03-07T17:00:00Z",
        }
        commit = {"sha": "abcdef123456"}

        with mock.patch(
            "ghp.cli._fetch_issues", side_effect=lambda *_: finish_together([issue])
        ), mock.patch(
            "ghp.cli._fetch_prs", side_effect=lambda *_: finish_together([pr])
        ), mock.patch(
            "ghp.cli._fetch_comment_endpoint",
            side_effect=lambda *args: finish_together(
                [issue_comment] if args[2] == "issues/comments" else [review_comment]
            ),
        ), mock.patch(
            "ghp.cli._fetch_commits", side_effect=lambda *_: finish_together([commit])
        ):
            issues, prs, comments, commits = cli._fetch_activity(
                "owner/repo", "token", 30, "2026-03-07T15:00:00Z"
            )

        self.assertEqual([issue], issues)
        self.assertEqual([pr], prs)
        self.assertEqual([review_comment, issue_comment], comments)
        self.assertEqual([commit], commits)

    def test_fetch_activity_skips_delta_endpoints_without_cutoff(self):
        with mock.patch(
            "ghp.cli._fetch_issues", return_value=[]
        ), mock.patch(
            "ghp.cli._fetch_prs", return_value=[]
        ), mock.patch(
            "ghp.cli._fetch_comment_endpoint"
        ) as fetch_comments, mock.patch(
            "ghp.cli._fetch_commits"
        ) as fetch_commits:
            self.assertEqual(
                ([], [], [], []),
                cli._fetch_activity("owner/repo", "token", 30, None),
            )

        fetch_comments.assert_not_called()
        fetch_commits.assert_not_called()


class StateHomeTests(unittest.TestCase):
    def test_state_home_uses_absolute_xdg_location(self):
        with mock.patch.dict(
            os.environ, {"XDG_STATE_HOME": "/custom/state"}
        ):
            self.assertEqual("/custom/state", cli._state_home())

    def test_state_home_uses_standard_fallback_for_relative_xdg_location(self):
        with mock.patch.dict(
            os.environ, {"XDG_STATE_HOME": "relative/state"}
        ), mock.patch(
            "ghp.cli.os.path.expanduser", return_value="/home/test-user"
        ):
            self.assertEqual("/home/test-user/.local/state", cli._state_home())

    def test_state_home_uses_standard_fallback_when_xdg_location_is_unset(self):
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch(
            "ghp.cli.os.path.expanduser", return_value="/home/test-user"
        ):
            self.assertEqual("/home/test-user/.local/state", cli._state_home())


class TimestampFileTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.repo_dir = os.path.join(self.tempdir.name, "repo")
        self.state_dir = os.path.join(self.tempdir.name, "state")
        os.mkdir(self.repo_dir)
        self.state_patcher = mock.patch(
            "ghp.cli._state_home", return_value=self.state_dir
        )
        self.cwd_patcher = mock.patch("os.getcwd", return_value=self.repo_dir)
        self.state_patcher.start()
        self.cwd_patcher.start()

    def tearDown(self):
        self.cwd_patcher.stop()
        self.state_patcher.stop()
        self.tempdir.cleanup()

    def test_load_last_update_timestamp_returns_none_when_missing(self):
        self.assertIsNone(cli._load_last_update_timestamp("owner/repo"))

    def test_save_and_load_last_update_timestamps_by_repo(self):
        cli._save_last_update_timestamp("owner/one", "2026-03-09T12:00:00Z")
        cli._save_last_update_timestamp("owner/two", "2026-03-09T13:00:00Z")

        self.assertEqual(
            "2026-03-09T12:00:00Z",
            cli._load_last_update_timestamp("OWNER/ONE"),
        )
        self.assertEqual(
            "2026-03-09T13:00:00Z",
            cli._load_last_update_timestamp("owner/two"),
        )
        self.assertTrue(
            os.path.exists(
                os.path.join(
                    self.state_dir, "ghp", "checkpoints", "owner%2Fone"
                )
            )
        )

    def test_legacy_checkpoint_requires_an_explicit_window(self):
        legacy_path = os.path.join(self.repo_dir, cli.LAST_UPDATE_FILENAME)
        with open(legacy_path, "w", encoding="utf-8") as fh:
            fh.write("2026-03-09T12:00:00Z\n")

        with self.assertRaisesRegex(ValueError, "does not identify its repository"):
            cli._load_last_update_timestamp("owner/repo")

        cli._save_last_update_timestamp("owner/repo", "2026-03-09T13:00:00Z")
        self.assertEqual(
            "2026-03-09T13:00:00Z",
            cli._load_last_update_timestamp("owner/repo"),
        )
        self.assertFalse(os.path.exists(legacy_path))

    def test_repository_map_is_migrated_before_local_file_is_removed(self):
        legacy_path = os.path.join(self.repo_dir, cli.LAST_UPDATE_FILENAME)
        with open(legacy_path, "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "owner/one": "2026-03-09T12:00:00Z",
                    "owner/two": "2026-03-09T13:00:00Z",
                },
                fh,
            )

        self.assertEqual(
            "2026-03-09T12:00:00Z",
            cli._load_last_update_timestamp("owner/one"),
        )
        self.assertEqual(
            "2026-03-09T13:00:00Z",
            cli._load_last_update_timestamp("owner/two"),
        )
        self.assertFalse(os.path.exists(legacy_path))

    def test_failed_atomic_replace_preserves_previous_checkpoint(self):
        cli._save_last_update_timestamp("owner/repo", "2026-03-09T12:00:00Z")

        with mock.patch("ghp.cli.os.replace", side_effect=OSError("boom")):
            with self.assertRaisesRegex(OSError, "boom"):
                cli._save_last_update_timestamp(
                    "owner/repo", "2026-03-09T13:00:00Z"
                )

        self.assertEqual(
            "2026-03-09T12:00:00Z",
            cli._load_last_update_timestamp("owner/repo"),
        )
        checkpoint_dir = os.path.join(self.state_dir, "ghp", "checkpoints")
        self.assertEqual(["owner%2Frepo"], os.listdir(checkpoint_dir))


class MainTests(unittest.TestCase):
    def setUp(self):
        self.state_tempdir = tempfile.TemporaryDirectory()
        self.state_patcher = mock.patch(
            "ghp.cli._state_home", return_value=self.state_tempdir.name
        )
        self.state_patcher.start()

    def tearDown(self):
        self.state_patcher.stop()
        self.state_tempdir.cleanup()

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
            "ghp.cli._fetch_activity", side_effect=cli.ApiError("boom")
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
                json.dump({"owner/repo": "2026-03-07T15:00:00Z"}, fh)

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
                cli._checkpoint_file_path("owner/repo"), "r", encoding="utf-8"
            ) as fh:
                self.assertEqual("2026-03-09T12:34:55Z", fh.read().strip())
            self.assertFalse(
                os.path.exists(os.path.join(tmpdir, cli.LAST_UPDATE_FILENAME))
            )

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

    def test_main_uses_upstream_remote(self):
        stdout = io.StringIO()

        with tempfile.TemporaryDirectory() as tmpdir, mock.patch.object(
            sys, "argv", ["ghp", "upstream"]
        ), mock.patch("os.getcwd", return_value=tmpdir), mock.patch(
            "ghp.cli._detect_repo", return_value="source/project"
        ) as detect_repo, mock.patch(
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
        detect_repo.assert_called_once_with("upstream")
        self.assertTrue(stdout.getvalue().startswith("source/project "))

    def test_main_accepts_upstream_with_positional_since(self):
        stdout = io.StringIO()

        with tempfile.TemporaryDirectory() as tmpdir, mock.patch.object(
            sys, "argv", ["ghp", "upstream", "1h"]
        ), mock.patch("os.getcwd", return_value=tmpdir), mock.patch(
            "ghp.cli._detect_repo", return_value="source/project"
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
                "source/project 2026-03-07T18:00:00Z since=2026-03-07T17:00:00Z"
            )
        )

    def test_main_reports_missing_upstream_remote(self):
        stderr = io.StringIO()

        with mock.patch.object(
            sys, "argv", ["ghp", "upstream"]
        ), mock.patch(
            "ghp.cli._detect_repo", return_value=None
        ), mock.patch(
            "sys.stderr", stderr
        ):
            code = cli.main()

        self.assertEqual(1, code)
        self.assertIn("git remote 'upstream'", stderr.getvalue())

    def test_main_does_not_share_checkpoint_between_repositories(self):
        stdout = io.StringIO()

        with tempfile.TemporaryDirectory() as tmpdir, mock.patch(
            "os.getcwd", return_value=tmpdir
        ), mock.patch(
            "ghp.cli._detect_repo",
            side_effect=lambda remote: {
                "origin": "owner/fork",
                "upstream": "source/project",
            }[remote],
        ), mock.patch(
            "ghp.cli._fetch_issues", return_value=[]
        ) as fetch_issues, mock.patch(
            "ghp.cli._fetch_prs", return_value=[]
        ), mock.patch(
            "ghp.cli._fetch_comments", return_value=[]
        ), mock.patch(
            "ghp.cli._fetch_commits", return_value=[]
        ), redirect_stdout(stdout):
            with mock.patch.object(
                sys, "argv", ["ghp"]
            ), mock.patch(
                "ghp.cli._utc_now",
                return_value=cli._parse_iso8601("2026-08-20T15:23:05Z"),
            ):
                self.assertEqual(0, cli.main())

            with mock.patch.object(
                sys, "argv", ["ghp", "upstream"]
            ), mock.patch(
                "ghp.cli._utc_now",
                return_value=cli._parse_iso8601("2026-08-20T16:10:35Z"),
            ):
                self.assertEqual(0, cli.main())

        self.assertIsNone(fetch_issues.call_args_list[1].args[3])


if __name__ == "__main__":
    unittest.main()
