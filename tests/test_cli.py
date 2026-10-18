import io
import json
import os
import sys
import tempfile
import threading
import unittest
from contextlib import ExitStack, redirect_stdout
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
            check=False,
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
            {
                "number": number,
                "pull_request": {"url": f"https://example.test/{number}"},
            }
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

    def test_merge_comments_sorts_and_limits_issue_and_review_comments(self):
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

        comments = cli._merge_comments([issue_comments, review_comments], 1)

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

        with (
            mock.patch(
                "ghp.cli._fetch_issues", side_effect=lambda *_: finish_together([issue])
            ),
            mock.patch(
                "ghp.cli._fetch_prs", side_effect=lambda *_: finish_together([pr])
            ),
            mock.patch(
                "ghp.cli._fetch_comment_endpoint",
                side_effect=lambda *args: finish_together(
                    [issue_comment]
                    if args[2] == "issues/comments"
                    else [review_comment]
                ),
            ),
            mock.patch(
                "ghp.cli._fetch_commits",
                side_effect=lambda *_: finish_together([commit]),
            ),
        ):
            issues, prs, comments, commits = cli._fetch_activity(
                "owner/repo", "token", 30, "2026-03-07T15:00:00Z"
            )

        self.assertEqual([issue], issues)
        self.assertEqual([pr], prs)
        self.assertEqual([review_comment, issue_comment], comments)
        self.assertEqual([commit], commits)

    def test_fetch_activity_skips_delta_endpoints_without_cutoff(self):
        with (
            mock.patch("ghp.cli._fetch_issues", return_value=[]),
            mock.patch("ghp.cli._fetch_prs", return_value=[]),
            mock.patch("ghp.cli._fetch_comment_endpoint") as fetch_comments,
            mock.patch("ghp.cli._fetch_commits") as fetch_commits,
        ):
            self.assertEqual(
                ([], [], [], []),
                cli._fetch_activity("owner/repo", "token", 30, None),
            )

        fetch_comments.assert_not_called()
        fetch_commits.assert_not_called()


class StateHomeTests(unittest.TestCase):
    def test_state_home_uses_absolute_xdg_location(self):
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": "/custom/state"}):
            self.assertEqual("/custom/state", cli._state_home())

    def test_state_home_uses_standard_fallback_for_relative_xdg_location(self):
        with (
            mock.patch.dict(os.environ, {"XDG_STATE_HOME": "relative/state"}),
            mock.patch("ghp.cli.os.path.expanduser", return_value="/home/test-user"),
        ):
            self.assertEqual("/home/test-user/.local/state", cli._state_home())

    def test_state_home_uses_standard_fallback_when_xdg_location_is_unset(self):
        with (
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch("ghp.cli.os.path.expanduser", return_value="/home/test-user"),
        ):
            self.assertEqual("/home/test-user/.local/state", cli._state_home())


class CheckpointTests(unittest.TestCase):
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

    def test_load_checkpoint_returns_none_when_missing(self):
        self.assertIsNone(cli._load_checkpoint("owner/repo"))

    def test_save_and_load_checkpoints_by_repo(self):
        cli._save_checkpoint("owner/one", "2026-03-09T12:00:00Z")
        cli._save_checkpoint("owner/two", "2026-03-09T13:00:00Z")

        self.assertEqual(
            "2026-03-09T12:00:00Z",
            cli._load_checkpoint("OWNER/ONE"),
        )
        self.assertEqual(
            "2026-03-09T13:00:00Z",
            cli._load_checkpoint("owner/two"),
        )
        self.assertTrue(
            os.path.exists(
                os.path.join(self.state_dir, "ghp", "checkpoints", "owner%2Fone")
            )
        )

    def test_legacy_checkpoint_requires_an_explicit_window(self):
        legacy_path = os.path.join(self.repo_dir, cli.LEGACY_CHECKPOINT_FILENAME)
        with open(legacy_path, "w", encoding="utf-8") as fh:
            fh.write("2026-03-09T12:00:00Z\n")

        with self.assertRaisesRegex(ValueError, "does not identify its repository"):
            cli._load_checkpoint("owner/repo")

        cli._save_checkpoint("owner/repo", "2026-03-09T13:00:00Z")
        self.assertEqual(
            "2026-03-09T13:00:00Z",
            cli._load_checkpoint("owner/repo"),
        )
        self.assertFalse(os.path.exists(legacy_path))

    def test_repository_map_is_migrated_before_local_file_is_removed(self):
        legacy_path = os.path.join(self.repo_dir, cli.LEGACY_CHECKPOINT_FILENAME)
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
            cli._load_checkpoint("owner/one"),
        )
        self.assertEqual(
            "2026-03-09T13:00:00Z",
            cli._load_checkpoint("owner/two"),
        )
        self.assertFalse(os.path.exists(legacy_path))

    def test_failed_atomic_replace_preserves_previous_checkpoint(self):
        cli._save_checkpoint("owner/repo", "2026-03-09T12:00:00Z")

        with (
            mock.patch("ghp.cli.os.replace", side_effect=OSError("boom")),
            self.assertRaisesRegex(OSError, "boom"),
        ):
            cli._save_checkpoint("owner/repo", "2026-03-09T13:00:00Z")

        self.assertEqual(
            "2026-03-09T12:00:00Z",
            cli._load_checkpoint("owner/repo"),
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

    def _run(self, argv, activity=None, now=None, cwd=None, fetch=None):
        stdout = io.StringIO()
        activity = activity or ([], [], [], [])
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(sys, "argv", ["ghp", *argv]))
            if fetch is None:
                fetch = stack.enter_context(
                    mock.patch("ghp.cli._fetch_activity", return_value=activity)
                )
            if now:
                stack.enter_context(
                    mock.patch("ghp.cli._utc_now", return_value=cli._parse_iso8601(now))
                )
            if cwd:
                stack.enter_context(mock.patch("os.getcwd", return_value=cwd))
            stack.enter_context(redirect_stdout(stdout))
            code = cli.main()
        return code, stdout.getvalue(), fetch

    def test_main_emits_version(self):
        stdout = io.StringIO()

        with (
            mock.patch.object(sys, "argv", ["ghp", "--version"]),
            mock.patch("ghp.cli._pkg_version", return_value="0.1.0"),
            redirect_stdout(stdout),
            self.assertRaises(SystemExit) as exc,
        ):
            cli.main()

        self.assertEqual(0, exc.exception.code)
        self.assertEqual("ghp 0.1.0\n", stdout.getvalue())

    def test_main_emits_json_error_and_nonzero_exit_on_api_failure(self):
        with mock.patch(
            "ghp.cli._fetch_activity", side_effect=cli.ApiError("boom")
        ) as fetch:
            code, output, _fetch = self._run(
                ["--json", "--repo", "owner/repo"], fetch=fetch
            )

        self.assertEqual(1, code)
        payload = json.loads(output)
        self.assertEqual("owner/repo", payload["repo"])
        self.assertEqual("boom", payload["error"])

    def test_main_uses_checkpoint_when_since_is_omitted(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            timestamp_path = os.path.join(tmpdir, cli.LEGACY_CHECKPOINT_FILENAME)
            with open(timestamp_path, "w", encoding="utf-8") as fh:
                json.dump({"owner/repo": "2026-03-07T15:00:00Z"}, fh)

            code, output, _fetch = self._run(["--repo", "owner/repo"], cwd=tmpdir)

        self.assertEqual(0, code)
        self.assertIn("since=2026-03-07T15:00:00Z", output)

    def test_main_saves_checkpoint_on_success(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            code, _output, _fetch = self._run(
                ["--repo", "owner/repo"],
                now="2026-03-09T12:34:56Z",
                cwd=tmpdir,
            )

            self.assertEqual(0, code)
            with open(
                cli._checkpoint_file_path("owner/repo"), "r", encoding="utf-8"
            ) as fh:
                self.assertEqual("2026-03-09T12:34:55Z", fh.read().strip())
            self.assertFalse(
                os.path.exists(os.path.join(tmpdir, cli.LEGACY_CHECKPOINT_FILENAME))
            )

    def test_main_includes_commits_in_json_output(self):
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

        code, output, _fetch = self._run(
            ["--json", "--repo", "owner/repo", "--since", "1h"],
            activity=([], [], [], commits),
        )

        self.assertEqual(0, code)
        payload = json.loads(output)
        self.assertEqual("abcdef123456", payload["commits"][0]["sha"])
        self.assertEqual("Fix bug", payload["commits"][0]["message"])

    def test_main_emits_compact_text_output(self):
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

        code, output, _fetch = self._run(
            ["--repo", "owner/repo", "--since", "1h", "--me", "@clod"],
            activity=([issue], [pr], [comment], commits),
            now="2026-03-07T18:00:00Z",
        )

        self.assertEqual(0, code)
        expected = """owner/repo 2026-03-07T18:00:00Z since=2026-03-07T17:00:00Z
issues 1
#7 open @alice trim output l:bug,p1 c:2
pr 1
#9 open,draft @bob feat->main ship less text c:1 rc:3
comments 1
#9 review @carol 2026-03-07T17:00:00Z: ping @clod with the latest diff
commits 1
abcdef1 @dave 2026-03-07T17:30:00Z tighten output
@clod 1
#9 review @carol 2026-03-07T17:00:00Z: ping @clod with the latest diff
"""
        self.assertEqual(expected, output)

    def test_main_accepts_positional_since_shorthand(self):
        code, output, _fetch = self._run(
            ["1h", "--repo", "owner/repo"], now="2026-03-07T18:00:00Z"
        )

        self.assertEqual(0, code)
        self.assertTrue(
            output.startswith(
                "owner/repo 2026-03-07T18:00:00Z since=2026-03-07T17:00:00Z"
            )
        )

    def test_main_uses_upstream_remote(self):
        with mock.patch(
            "ghp.cli._detect_repo", return_value="source/project"
        ) as detect_repo:
            code, output, _fetch = self._run(["upstream"])

        self.assertEqual(0, code)
        detect_repo.assert_called_once_with("upstream")
        self.assertTrue(output.startswith("source/project "))

    def test_main_accepts_upstream_with_positional_since(self):
        with mock.patch("ghp.cli._detect_repo", return_value="source/project"):
            code, output, _fetch = self._run(
                ["upstream", "1h"], now="2026-03-07T18:00:00Z"
            )

        self.assertEqual(0, code)
        self.assertTrue(
            output.startswith(
                "source/project 2026-03-07T18:00:00Z since=2026-03-07T17:00:00Z"
            )
        )

    def test_main_reports_missing_upstream_remote(self):
        stderr = io.StringIO()

        with (
            mock.patch.object(sys, "argv", ["ghp", "upstream"]),
            mock.patch("ghp.cli._detect_repo", return_value=None),
            mock.patch("sys.stderr", stderr),
        ):
            code = cli.main()

        self.assertEqual(1, code)
        self.assertIn("git remote 'upstream'", stderr.getvalue())

    def test_main_does_not_share_checkpoint_between_repositories(self):
        with (
            mock.patch(
                "ghp.cli._detect_repo",
                side_effect=lambda remote: {
                    "origin": "owner/fork",
                    "upstream": "source/project",
                }[remote],
            ),
            mock.patch(
                "ghp.cli._fetch_activity", return_value=([], [], [], [])
            ) as fetch,
        ):
            code, _output, _fetch = self._run(
                [], now="2026-08-20T15:23:05Z", fetch=fetch
            )
            self.assertEqual(0, code)
            code, _output, _fetch = self._run(
                ["upstream"], now="2026-08-20T16:10:35Z", fetch=fetch
            )
            self.assertEqual(0, code)

        self.assertIsNone(fetch.call_args_list[1].args[3])


def _notification(number, subject_type="Issue", unread=True, repo="owner/repo"):
    path = {"Issue": "issues", "PullRequest": "pulls", "Commit": "commits"}.get(
        subject_type
    )
    return {
        "id": str(number),
        "unread": unread,
        "reason": "mention",
        "updated_at": "2026-03-07T17:30:00Z",
        "subject": {
            "title": f"subject {number}",
            "type": subject_type,
            "url": (
                f"https://api.github.com/repos/{repo}/{path}/{number}" if path else None
            ),
        },
        "repository": {
            "full_name": repo,
            "html_url": f"https://github.com/{repo}",
        },
    }


class NotificationTests(unittest.TestCase):
    def test_fetch_notifications_requests_unread_for_the_account(self):
        with mock.patch("ghp.cli._api", return_value=[]) as api:
            cli._fetch_notifications(None, "token", 30, None)

        api.assert_called_once_with(
            "notifications", "token", {"per_page": 30, "page": 1}
        )

    def test_fetch_notifications_includes_read_ones_since_cutoff_for_a_repo(self):
        with mock.patch("ghp.cli._api", return_value=[]) as api:
            cli._fetch_notifications("owner/repo", "token", 5, "2026-03-07T17:00:00Z")

        api.assert_called_once_with(
            "repos/owner/repo/notifications",
            "token",
            {"all": "true", "since": "2026-03-07T17:00:00Z", "per_page": 5, "page": 1},
        )

    def test_fetch_notifications_paginates_at_the_api_page_maximum(self):
        pages = [
            [_notification(number) for number in range(50)],
            [_notification(number) for number in range(50, 100)],
        ]

        with mock.patch("ghp.cli._api", side_effect=pages) as api:
            notifications = cli._fetch_notifications(None, "token", 60, None)

        self.assertEqual(60, len(notifications))
        self.assertEqual(
            [50, 50], [call.args[2]["per_page"] for call in api.call_args_list]
        )

    def test_notification_lines_identify_the_subject(self):
        self.assertEqual(
            "#7 pr mention 2026-03-07T17:30:00Z subject 7",
            cli._fmt_notification(_notification(7, "PullRequest")),
        )
        self.assertEqual(
            "#8 issue mention,read 2026-03-07T17:30:00Z subject 8",
            cli._fmt_notification(_notification(8, unread=False)),
        )
        self.assertEqual(
            "abcdef0 commit mention 2026-03-07T17:30:00Z subject abcdef0123",
            cli._fmt_notification(_notification("abcdef0123", "Commit")),
        )
        self.assertEqual(
            "discussion mention 2026-03-07T17:30:00Z subject 9",
            cli._fmt_notification(_notification(9, "Discussion")),
        )

    def test_notification_urls_point_at_the_website(self):
        self.assertEqual(
            "https://github.com/owner/repo/pull/7",
            cli._notification_url(_notification(7, "PullRequest")),
        )
        self.assertEqual(
            "https://github.com/owner/repo",
            cli._notification_url(_notification(9, "Discussion")),
        )


class InboxTests(unittest.TestCase):
    def _run(self, argv, notifications=(), token="token"):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(sys, "argv", ["ghp", *argv]))
            stack.enter_context(mock.patch("ghp.cli._get_token", return_value=token))
            stack.enter_context(
                mock.patch(
                    "ghp.cli._utc_now",
                    return_value=cli._parse_iso8601("2026-03-07T18:00:00Z"),
                )
            )
            self.detect_repo = stack.enter_context(mock.patch("ghp.cli._detect_repo"))
            self.save_checkpoint = stack.enter_context(
                mock.patch("ghp.cli._save_checkpoint")
            )
            fetch = stack.enter_context(
                mock.patch(
                    "ghp.cli._fetch_notifications", return_value=list(notifications)
                )
            )
            stack.enter_context(redirect_stdout(stdout))
            stack.enter_context(mock.patch("sys.stderr", stderr))
            code = cli.main()
        return code, stdout.getvalue(), stderr.getvalue(), fetch

    def test_inbox_groups_notifications_by_repository(self):
        code, output, _stderr, fetch = self._run(
            ["inbox"],
            [
                _notification(1),
                _notification(2, "PullRequest", repo="other/project"),
                _notification(3),
            ],
        )

        self.assertEqual(0, code)
        self.assertEqual(
            [
                "inbox 2026-03-07T18:00:00Z",
                "owner/repo 2",
                "#1 issue mention 2026-03-07T17:30:00Z subject 1",
                "#3 issue mention 2026-03-07T17:30:00Z subject 3",
                "other/project 1",
                "#2 pr mention 2026-03-07T17:30:00Z subject 2",
            ],
            output.splitlines(),
        )
        fetch.assert_called_once_with(None, "token", 30, None)

    def test_inbox_ignores_the_checkout_and_checkpoints(self):
        code, output, _stderr, _fetch = self._run(["inbox"])

        self.assertEqual(0, code)
        self.assertEqual(["inbox 2026-03-07T18:00:00Z", "none"], output.splitlines())
        self.detect_repo.assert_not_called()
        self.save_checkpoint.assert_not_called()

    def test_inbox_accepts_since_and_repo(self):
        code, output, _stderr, fetch = self._run(
            ["inbox", "1h", "--repo", "owner/repo"]
        )

        self.assertEqual(0, code)
        self.assertTrue(
            output.startswith(
                "inbox 2026-03-07T18:00:00Z repo=owner/repo since=2026-03-07T17:00:00Z"
            )
        )
        fetch.assert_called_once_with("owner/repo", "token", 30, "2026-03-07T17:00:00Z")

    def test_inbox_emits_json(self):
        code, output, _stderr, _fetch = self._run(
            ["inbox", "--json"], [_notification(7, "PullRequest", unread=False)]
        )

        self.assertEqual(0, code)
        self.assertEqual(
            {
                "repo": None,
                "timestamp": "2026-03-07T18:00:00Z",
                "since": None,
                "notifications": [
                    {
                        "id": "7",
                        "repo": "owner/repo",
                        "type": "pr",
                        "number": "7",
                        "title": "subject 7",
                        "reason": "mention",
                        "unread": False,
                        "updated_at": "2026-03-07T17:30:00Z",
                        "url": "https://github.com/owner/repo/pull/7",
                    }
                ],
            },
            json.loads(output),
        )

    def test_inbox_requires_a_token(self):
        code, _output, stderr, fetch = self._run(["inbox"], token=None)

        self.assertEqual(1, code)
        self.assertIn("inbox needs a GitHub token", stderr)
        fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main()


def _search_pr(repo, number, created_at, user="contributor"):
    return {
        "number": number,
        "title": f"pr {number}",
        "user": {"login": user},
        "created_at": created_at,
        "html_url": f"https://github.com/{repo}/pull/{number}",
        "repository_url": f"https://api.github.com/repos/{repo}",
    }


def _repo(full_name, fork=False, archived=False):
    return {"full_name": full_name, "fork": fork, "archived": archived}


class OwnedTests(unittest.TestCase):
    """Routes each GitHub API path to a canned response and rejects any other path."""

    def setUp(self):
        self.responses = {
            ("user", ()): {"login": "me"},
            ("user/memberships/orgs", (("state", "active"),)): [
                {"organization": {"login": "team"}, "role": "admin"},
                {"organization": {"login": "club"}, "role": "member"},
            ],
            ("user/repos", (("affiliation", "owner"),)): [
                _repo("me/watched"),
                _repo("me/Quiet"),
                _repo("me/fork", fork=True),
                _repo("me/old", archived=True),
            ],
            ("orgs/team/repos", (("type", "sources"),)): [_repo("team/tool")],
            ("user/subscriptions", ()): [
                {"full_name": "me/watched"},
                {"full_name": "other/project"},
            ],
            (
                "search/issues",
                (("q", "is:pr is:open archived:false user:me"),),
            ): {
                "items": [
                    _search_pr("me/watched", 5, "2026-03-07T12:00:00Z"),
                    _search_pr("me/watched", 4, "2026-03-06T12:00:00Z"),
                    _search_pr("me/Quiet", 9, "2026-03-05T12:00:00Z"),
                ]
            },
            (
                "search/issues",
                (("q", "is:pr is:open archived:false org:team"),),
            ): {"items": [_search_pr("team/tool", 2, "2026-03-01T12:00:00Z", "me")]},
            ("repos/me/watched/actions/runs", (("status", "action_required"),)): {
                "workflow_runs": [{"head_sha": "fork-head", "pull_requests": []}]
            },
            ("repos/me/Quiet/actions/runs", (("status", "action_required"),)): {
                "workflow_runs": []
            },
            ("repos/team/tool/actions/runs", (("status", "action_required"),)): {
                "workflow_runs": []
            },
            ("repos/me/watched/pulls/5", ()): {"head": {"sha": "fork-head"}},
            ("repos/me/watched/pulls/4", ()): {"head": {"sha": "approved-head"}},
        }
        self.calls = []

    def _api(self, path, token, params=None):
        params = dict(params or {})
        page = params.pop("page", 1)
        params.pop("per_page", None)
        key = (path, tuple(sorted(params.items())))
        self.calls.append(key)
        if key not in self.responses:
            raise AssertionError(f"unexpected API call: {key}")
        return self.responses[key] if page == 1 else []

    def _run(self, argv):
        stdout = io.StringIO()
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(sys, "argv", ["ghp", *argv]))
            stack.enter_context(mock.patch("ghp.cli._get_token", return_value="token"))
            stack.enter_context(mock.patch("ghp.cli._api", side_effect=self._api))
            stack.enter_context(
                mock.patch(
                    "ghp.cli._utc_now",
                    return_value=cli._parse_iso8601("2026-03-07T18:00:00Z"),
                )
            )
            self.detect_repo = stack.enter_context(mock.patch("ghp.cli._detect_repo"))
            stack.enter_context(redirect_stdout(stdout))
            code = cli.main()
        return code, stdout.getvalue()

    def test_owned_lists_waiting_fork_prs_and_unwatched_repositories(self):
        code, output = self._run(["owned"])

        self.assertEqual(0, code)
        self.assertEqual(
            [
                "owned 2026-03-07T18:00:00Z owners=me,team",
                "awaiting-approval 1",
                "me/watched#5 @contributor 2026-03-07T12:00:00Z pr 5",
                "unwatched 2",
                "me/Quiet",
                "team/tool",
            ],
            output.splitlines(),
        )
        self.detect_repo.assert_not_called()

    def test_owned_fetches_pr_heads_only_in_repositories_with_waiting_runs(self):
        self._run(["owned"])

        pulls = sorted(path for path, _params in self.calls if "/pulls/" in path)
        self.assertEqual(
            ["repos/me/watched/pulls/4", "repos/me/watched/pulls/5"], pulls
        )

    def test_owned_emits_json(self):
        code, output = self._run(["owned", "--json"])

        self.assertEqual(0, code)
        self.assertEqual(
            {
                "timestamp": "2026-03-07T18:00:00Z",
                "owners": ["me", "team"],
                "awaiting_approval": [
                    {
                        "repo": "me/watched",
                        "number": 5,
                        "title": "pr 5",
                        "user": "contributor",
                        "created_at": "2026-03-07T12:00:00Z",
                        "url": "https://github.com/me/watched/pull/5",
                    }
                ],
                "unwatched": ["me/Quiet", "team/tool"],
            },
            json.loads(output),
        )

    def test_owned_sorts_waiting_prs_oldest_first(self):
        self.responses[("repos/me/watched/pulls/4", ())] = {
            "head": {"sha": "fork-head"}
        }

        _code, output = self._run(["owned"])

        self.assertEqual(
            ["me/watched#4", "me/watched#5"],
            [line.split()[0] for line in output.splitlines()[2:4]],
        )

    def test_api_all_reads_every_page(self):
        pages = [{"items": [{"n": n} for n in range(100)]}, {"items": [{"n": 100}]}]

        with mock.patch("ghp.cli._api", side_effect=pages) as api:
            items = cli._api_all("search/issues", "token", {"q": "x"}, "items")

        self.assertEqual(101, len(items))
        self.assertEqual([1, 2], [call.args[2]["page"] for call in api.call_args_list])

    def test_owned_rejects_time_and_repo(self):
        for argv in (["owned", "1h"], ["owned", "--repo", "me/watched"]):
            with (
                self.subTest(argv=argv),
                mock.patch("sys.stderr", io.StringIO()),
                self.assertRaises(SystemExit),
            ):
                self._run(argv)
