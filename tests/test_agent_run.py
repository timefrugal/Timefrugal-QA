"""
Tests for qa_agent.agent.run()'s exit-code precedence.

Uses stdlib unittest (no pytest / test framework is set up in this repo yet),
following the convention established in tests/test_agent.py (which covers
get_changed_files/read_file_contents; this file covers run()'s overall
return-code formula).

run() is an integration-style orchestrator (git diff -> static analysis ->
AI review -> report), so this mocks the pipeline at its natural seams
(get_changed_files, read_file_contents, static_analysis.run_all,
ai_review.review_code, and the CI-mode reporting functions in pr_reporter)
rather than extracting the two-line precedence expression
(`1 if blocked else (2 if errored else 0)`) into its own function purely to
make it more unit-testable -- that refactor wasn't otherwise warranted, and
mocking at the seams still exercises the real formula inside run() as shipped.
"""
import unittest
from unittest import mock

from qa_agent import agent, pr_reporter
from qa_agent.ai_review import AIReview
from qa_agent.static_analysis import AnalysisResults, Finding


def _blocking_static_results(errors=None):
    return AnalysisResults(
        findings=[Finding(
            tool="bandit", severity="CRITICAL", category="security",
            file="app.py", line=1, message="fake critical finding",
        )],
        errors=errors or [],
    )


def _clean_static_results(errors=None):
    return AnalysisResults(findings=[], errors=errors or [])


class TestAgentRunExitCodePrecedence(unittest.TestCase):
    """
    run()'s documented contract: 0 = all clear, 1 = blocking issues found,
    2 = fatal/tool error (CI mode only). `blocked` must take priority over
    `errored` -- a real blocking finding is always reported as 1, never
    downgraded to 2 just because a tool also failed alongside it.
    """

    def setUp(self):
        self._patches = [
            mock.patch.object(agent, "get_changed_files", return_value=["app.py"]),
            mock.patch.object(agent, "read_file_contents", return_value={"app.py": "print(1)\n"}),
            mock.patch.object(pr_reporter, "post_pr_comment", return_value=True),
            mock.patch.object(pr_reporter, "set_commit_status", return_value=True),
            mock.patch.object(pr_reporter, "write_step_summary", return_value=None),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)

    def _run_ci(self, static_results, ai_review):
        with mock.patch.object(agent, "run_all", return_value=static_results), \
             mock.patch.object(agent, "review_code", return_value=ai_review), \
             mock.patch.object(agent, "generate_tests", return_value=""):
            return agent.run(
                mode="ci",
                pr_number="1",
                project_root=".",
                generate_test_cases=False,
            )

    def test_no_issues_no_errors_returns_0(self):
        rc = self._run_ci(_clean_static_results(), AIReview())
        self.assertEqual(rc, 0)

    def test_blocking_issue_alone_returns_1(self):
        rc = self._run_ci(_blocking_static_results(), AIReview())
        self.assertEqual(rc, 1)

    def test_tool_error_alone_in_ci_mode_returns_2(self):
        rc = self._run_ci(_clean_static_results(errors=["bandit: not found"]), AIReview())
        self.assertEqual(rc, 2)

    def test_blocked_takes_priority_over_errored_returns_1_not_2(self):
        rc = self._run_ci(
            _blocking_static_results(errors=["bandit: not found"]), AIReview()
        )
        self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()
