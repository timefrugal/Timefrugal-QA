"""
Tests for qa_agent.pr_reporter.

Uses stdlib unittest (no pytest / test framework is set up in this repo yet),
following the convention established in tests/test_repo_config.py.
"""
import contextlib
import io
import unittest
from unittest import mock

from qa_agent import config, pr_reporter
from qa_agent.ai_review import AIFinding, AIReview
from qa_agent.static_analysis import AnalysisResults, Finding


class _FakeResponse:
    def __init__(self, status_code=201, text=""):
        self.status_code = status_code
        self.text = text

    def json(self):
        return {}


class TestSetCommitStatusPrecedence(unittest.TestCase):
    """
    C1 regression: set_commit_status's state computation must give `blocked`
    strict priority over `errored`. The bug this guards against: an earlier
    version computed state such that `errored=True` could produce GitHub
    status "error" even when `blocked=True`, which is the wrong signal --
    "failure" (a real blocking finding) must never be downgraded/reclassified
    to "error" (tool trouble) just because a tool also failed to run. This was
    previously only checked with an ad hoc manual script; this is the
    permanent regression test.
    """

    def setUp(self):
        # set_commit_status short-circuits (returns False, no HTTP call) unless
        # GITHUB_REPOSITORY and GITHUB_SHA are set -- patch config directly
        # rather than mutating the environment.
        patcher_repo = mock.patch.object(config, "GITHUB_REPOSITORY", "owner/repo")
        patcher_sha = mock.patch.object(config, "GITHUB_SHA", "deadbeef")
        patcher_repo.start()
        patcher_sha.start()
        self.addCleanup(patcher_repo.stop)
        self.addCleanup(patcher_sha.stop)

    def _post_and_capture_state(self, blocked, errored):
        captured = {}

        def fake_post(url, headers=None, json=None, timeout=None):
            captured["payload"] = json
            return _FakeResponse(201)

        with mock.patch("qa_agent.pr_reporter.requests.post", side_effect=fake_post):
            ok = pr_reporter.set_commit_status(blocked=blocked, errored=errored)

        self.assertTrue(ok)
        return captured["payload"]["state"]

    def test_blocked_takes_priority_over_errored_in_commit_status(self):
        state = self._post_and_capture_state(blocked=True, errored=True)
        self.assertEqual(state, "failure")
        self.assertNotEqual(state, "error")

    def test_blocked_alone_is_failure(self):
        state = self._post_and_capture_state(blocked=True, errored=False)
        self.assertEqual(state, "failure")

    def test_errored_alone_is_error(self):
        state = self._post_and_capture_state(blocked=False, errored=True)
        self.assertEqual(state, "error")

    def test_neither_is_success(self):
        state = self._post_and_capture_state(blocked=False, errored=False)
        self.assertEqual(state, "success")


class TestSetCommitStatusLogging(unittest.TestCase):
    """
    jarvis-infra#286 follow-up: set_commit_status previously had no logging at
    all on failure (unlike post_pr_comment), which made its apparent 100%
    failure rate at creating the custom GitHub status context silently
    invisible. This asserts both the success and failure paths now log to
    the right stream and return the right bool.
    """

    def setUp(self):
        patcher_repo = mock.patch.object(config, "GITHUB_REPOSITORY", "owner/repo")
        patcher_sha = mock.patch.object(config, "GITHUB_SHA", "deadbeef")
        patcher_repo.start()
        patcher_sha.start()
        self.addCleanup(patcher_repo.stop)
        self.addCleanup(patcher_sha.stop)

    def test_failure_response_logs_to_stderr_and_returns_false(self):
        fake_resp = _FakeResponse(403, text="some error body")
        stderr = io.StringIO()
        with mock.patch("qa_agent.pr_reporter.requests.post", return_value=fake_resp):
            with contextlib.redirect_stderr(stderr):
                ok = pr_reporter.set_commit_status(blocked=False, errored=False)

        self.assertFalse(ok)
        output = stderr.getvalue()
        self.assertIn("403", output)
        self.assertIn("deadbeef", output)

    def test_success_response_logs_to_stdout_and_returns_true(self):
        fake_resp = _FakeResponse(201)
        stdout = io.StringIO()
        with mock.patch("qa_agent.pr_reporter.requests.post", return_value=fake_resp):
            with contextlib.redirect_stdout(stdout):
                ok = pr_reporter.set_commit_status(blocked=False, errored=False)

        self.assertTrue(ok)
        output = stdout.getvalue()
        self.assertIn("deadbeef", output)
        self.assertIn("success", output)


def _static_finding(severity, category="quality"):
    return Finding(
        tool="pylint", severity=severity, category=category,
        file="a.py", line=1, message="dangerous-default-value", rule_id="W0102",
    )


def _ai_finding(severity):
    return AIFinding(
        severity=severity, category="bug", file="a.py", line=1,
        message="boom", suggestion="fix it",
    )


class TestBuildCommentBlockedHeader(unittest.TestCase):
    """
    jarvis-infra#323 regression: the BLOCKED header hardcoded "Critical/High
    issues require attention before merge" regardless of which cutoff actually
    fired. On jarvis-infra (`block_merge_threshold: MEDIUM`, deliberately
    stricter because it runs live infra) PR #312 got that header above a table
    showing 0 Critical and 0 High, which reads as a broken gate -- the issue
    was filed against a gate that was in fact working exactly as configured.
    The header must now name the real effective cutoff.
    """

    def setUp(self):
        patcher = mock.patch.object(config, "BLOCK_MERGE_THRESHOLD", config.SEVERITY_HIGH)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_default_threshold_critical_high_block_keeps_todays_wording(self):
        static = AnalysisResults(findings=[_static_finding("HIGH")])
        ai = AIReview()
        body = pr_reporter._build_comment(static, ai, "")
        self.assertIn(
            "## 🔴 Timefrugal-QA — BLOCKED: Critical/High issues require attention before merge",
            body,
        )
        self.assertNotIn("gates merges at", body)

    def test_medium_threshold_block_with_zero_critical_high_names_medium(self):
        static = AnalysisResults(
            findings=[_static_finding("MEDIUM"), _static_finding("MEDIUM")],
            block_merge_threshold="MEDIUM",
        )
        ai = AIReview()
        body = pr_reporter._build_comment(static, ai, "")
        self.assertIn(
            "BLOCKED: Medium-or-above issues require attention before merge", body
        )
        self.assertNotIn("Critical/High issues require attention", body)
        self.assertIn("| 🔴 Critical | 0 |", body)
        self.assertIn("| 🟠 High     | 0 |", body)
        self.assertIn("**MEDIUM**", body)
        self.assertIn(".timefrugal-qa.yml", body)

    def test_ai_only_block_on_a_medium_repo_still_says_critical_high(self):
        static = AnalysisResults(
            findings=[_static_finding("LOW")], block_merge_threshold="MEDIUM"
        )
        ai = AIReview(findings=[_ai_finding("CRITICAL")], ai_blocking=True)
        body = pr_reporter._build_comment(static, ai, "")
        self.assertIn("Critical/High", body)
        self.assertNotIn("gates merges at", body)

    def test_not_blocked_path_is_unchanged(self):
        static = AnalysisResults()
        ai = AIReview()
        body = pr_reporter._build_comment(static, ai, "")
        self.assertIn("## ✅ Timefrugal-QA — All checks passed", body)
        self.assertNotIn("BLOCKED", body)
        self.assertNotIn("gates merges at", body)

    def test_blocked_and_errored_keeps_the_tool_warnings_suffix_on_the_header(self):
        static = AnalysisResults(
            findings=[_static_finding("HIGH")], errors=["bandit: not found"]
        )
        ai = AIReview()
        body = pr_reporter._build_comment(static, ai, "")
        self.assertIn(
            "BLOCKED: Critical/High issues require attention before merge "
            "(Note: some analysis tools also failed to complete",
            body,
        )


class TestBuildCommentStaysUnderGitHubCommentLimit(unittest.TestCase):
    """Regression coverage for a real HTTP 422 seen live 2026-09-07:
    "Body is too long (maximum is 65536 characters)" on a PR whose static
    analysis produced 598 medium + 356 low + 1143 info findings on one
    file. post_pr_comment silently returned False and NOTHING was ever
    posted -- a real QA result (pass or fail) vanishing without a trace.
    Two independent guards now exist: capping the inline medium/low/info
    findings list (the actual usual cause), and a last-resort whole-body
    truncation (_truncate_to_github_limit) covering whatever the cap
    doesn't (a huge AI review or generated-test block)."""

    def test_body_never_exceeds_githubs_hard_limit_even_with_massive_findings(self):
        static = AnalysisResults(
            findings=[_static_finding("MEDIUM") for _ in range(5000)]
        )
        ai = AIReview()
        body = pr_reporter._build_comment(static, ai, "")
        self.assertLessEqual(len(body), pr_reporter._GITHUB_COMMENT_MAX_CHARS)

    def test_small_findings_list_is_not_truncated_or_capped(self):
        static = AnalysisResults(findings=[_static_finding("MEDIUM")])
        ai = AIReview()
        body = pr_reporter._build_comment(static, ai, "")
        self.assertNotIn("Report truncated", body)
        self.assertNotIn("more not shown here", body)

    def test_lower_findings_list_over_the_cap_shows_omitted_count_note(self):
        count = pr_reporter._MAX_LOWER_FINDINGS_SHOWN + 25
        static = AnalysisResults(
            findings=[_static_finding("LOW") for _ in range(count)]
        )
        ai = AIReview()
        body = pr_reporter._build_comment(static, ai, "")
        self.assertIn("...and 25 more not shown here", body)
        # Exactly the cap's worth of individual finding lines were rendered,
        # not the full 175 -- confirms the cap actually limits the list
        # rather than merely appending a note on top of everything.
        self.assertEqual(
            body.count("**pylint** dangerous-default-value"),
            pr_reporter._MAX_LOWER_FINDINGS_SHOWN,
        )

    def test_lower_findings_list_at_or_under_the_cap_has_no_omitted_note(self):
        static = AnalysisResults(
            findings=[_static_finding("LOW") for _ in range(pr_reporter._MAX_LOWER_FINDINGS_SHOWN)]
        )
        ai = AIReview()
        body = pr_reporter._build_comment(static, ai, "")
        self.assertNotIn("more not shown here", body)

    def test_whole_body_safety_net_truncates_an_oversized_ai_review_alone(self):
        # Even with a SINGLE static finding (well under the list cap), a
        # sufficiently large AI review/summary must still not blow past
        # GitHub's limit -- the last-resort truncation must not depend on
        # the findings-list cap being the only possible cause.
        static = AnalysisResults(findings=[_static_finding("LOW")])
        ai = AIReview(summary="x" * 200_000)
        body = pr_reporter._build_comment(static, ai, "")
        self.assertLessEqual(len(body), pr_reporter._GITHUB_COMMENT_MAX_CHARS)
        self.assertIn("Report truncated", body)


class TestBuildCommentCreditsTheProviderThatActuallyAnswered(unittest.TestCase):
    """Added 2026-09-07 alongside the QA_FALLBACK_MODEL retry fix -- once a
    repo configures more than one provider (or a QA_FALLBACK_MODEL), which
    one actually served a given review is no longer a foregone conclusion.
    The comment must say which one it was, not silently attribute every
    review to Groq the way the original hardcoded footer did."""

    def test_provider_line_shown_when_ai_review_succeeded(self):
        static = AnalysisResults()
        ai = AIReview(summary="looks fine", provider="fallback", provider_model="qwen3.8:27b")
        body = pr_reporter._build_comment(static, ai, "")
        self.assertIn("_AI review served by **fallback** (`qwen3.8:27b`)_", body)
        self.assertIn("AI review via fallback", body)  # footer
        self.assertNotIn("Free AI via Groq", body)

    def test_provider_line_absent_when_no_provider_succeeded(self):
        static = AnalysisResults()
        ai = AIReview(errors=["No configured AI provider returned valid JSON (last error: ...)"])
        body = pr_reporter._build_comment(static, ai, "")
        self.assertNotIn("AI review served by", body)
        self.assertIn("· Free AI · Open-source analysis tools_", body)  # generic footer fallback


if __name__ == "__main__":
    unittest.main()
