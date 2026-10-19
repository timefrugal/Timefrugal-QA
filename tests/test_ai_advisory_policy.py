"""
Tests for the advisory-unless-security AI policy: AIReview.has_blocking_issues scope, AIReview.blocking_errors,
review_code wiring from RepoConfig, the run() exit code, and the PR comment header.
"""
import unittest
from unittest import mock

from qa_agent import agent, ai_review as air, pr_reporter
from qa_agent.ai_review import AIFinding, AIReview
from qa_agent.repo_config import RepoConfig
from qa_agent.static_analysis import AnalysisResults


_REAL_SET_COMMIT_STATUS = pr_reporter.set_commit_status  # setUp below patches the module attribute


def _f(sev, cat):
    return AIFinding(severity=sev, category=cat, file="a.py", line=1, message="m", suggestion="s")


class TestHasBlockingIssuesScope(unittest.TestCase):
    def test_all_scope_blocks_on_any_critical_or_high(self):
        self.assertTrue(AIReview(findings=[_f("HIGH", "bug")], ai_blocking=True).has_blocking_issues)

    def test_security_scope_ignores_non_security_critical_and_high(self):
        r = AIReview(findings=[_f("CRITICAL", "bug"), _f("HIGH", "design")], ai_blocking=True,
                     ai_blocking_scope="security")
        self.assertFalse(r.has_blocking_issues)

    def test_security_scope_blocks_on_a_security_critical_or_high(self):
        for sev in ("CRITICAL", "HIGH"):
            r = AIReview(findings=[_f(sev, "security")], ai_blocking=True, ai_blocking_scope="security")
            self.assertTrue(r.has_blocking_issues, sev)

    def test_security_scope_does_not_block_on_a_medium_security_finding(self):
        r = AIReview(findings=[_f("MEDIUM", "security")], ai_blocking=True, ai_blocking_scope="security")
        self.assertFalse(r.has_blocking_issues)

    def test_category_match_tolerates_case_and_whitespace(self):
        r = AIReview(findings=[_f("HIGH", " Security ")], ai_blocking=True, ai_blocking_scope="security")
        self.assertTrue(r.has_blocking_issues)

    def test_security_scope_fails_closed_on_synonyms_unknowns_and_non_strings(self):
        for cat in ("vulnerability", "Security Vulnerability", "security/injection", "secrets", "injection", "xss",
                    "auth", "", None, ["security"], 7):
            r = AIReview(findings=[_f("CRITICAL", cat)], ai_blocking=True, ai_blocking_scope="security")
            self.assertTrue(r.has_blocking_issues, repr(cat))  # and never raises

    def test_security_scope_stays_advisory_for_every_known_non_security_category(self):
        for cat in ("bug", "Architecture", " design ", "performance", "quality", "test"):
            r = AIReview(findings=[_f("CRITICAL", cat)], ai_blocking=True, ai_blocking_scope="security")
            self.assertFalse(r.has_blocking_issues, repr(cat))

    def test_blocking_off_never_blocks_whatever_the_scope(self):
        r = AIReview(findings=[_f("CRITICAL", "security")], ai_blocking=False, ai_blocking_scope="security")
        self.assertFalse(r.has_blocking_issues)


class TestBlockingErrors(unittest.TestCase):
    def test_errors_block_by_default(self):
        self.assertEqual(AIReview(errors=["x"]).blocking_errors, ["x"])

    def test_errors_are_non_blocking_when_the_repo_opts_out_but_still_recorded(self):
        r = AIReview(errors=["x"], ai_errors_block=False)
        self.assertEqual(r.blocking_errors, [])
        self.assertEqual(r.errors, ["x"])


class TestReviewCodeCarriesRepoConfig(unittest.TestCase):
    def test_flags_are_copied_from_the_repo_config_even_when_no_files_are_given(self):
        cfg = RepoConfig(ai_blocking=True, ai_blocking_scope="security", ai_errors_block=False)
        review = air.review_code({}, "python", "", repo_config=cfg)
        self.assertTrue(review.ai_blocking)
        self.assertEqual(review.ai_blocking_scope, "security")
        self.assertFalse(review.ai_errors_block)
        self.assertTrue(review.errors)  # "No file contents provided" is still reported

    def test_errors_block_false_is_carried_even_when_ai_blocking_is_off(self):
        review = air.review_code({}, "python", "", repo_config=RepoConfig(ai_blocking=False, ai_errors_block=False))
        self.assertFalse(review.ai_errors_block)

    def test_no_repo_config_keeps_the_original_defaults(self):
        review = air.review_code({}, "python", "")
        self.assertFalse(review.ai_blocking)
        self.assertEqual(review.ai_blocking_scope, "all")
        self.assertTrue(review.ai_errors_block)


class TestRunExitCodeAndComment(unittest.TestCase):
    def setUp(self):
        for p in (
            mock.patch.object(agent, "get_changed_files", return_value=["app.py"]),
            mock.patch.object(agent, "read_file_contents", return_value={"app.py": "print(1)\n"}),
            mock.patch.object(pr_reporter, "post_pr_comment", return_value=True),
            mock.patch.object(pr_reporter, "set_commit_status", return_value=True),
            mock.patch.object(pr_reporter, "write_step_summary", return_value=None),
        ):
            p.start()
            self.addCleanup(p.stop)

    def _rc(self, static, ai):
        with mock.patch.object(agent, "run_all", return_value=static), \
             mock.patch.object(agent, "review_code", return_value=ai), \
             mock.patch.object(agent, "generate_tests", return_value=""):
            return agent.run(mode="ci", pr_number="1", project_root=".", generate_test_cases=False)

    def test_an_ai_failure_alone_is_exit_2_by_default(self):
        self.assertEqual(self._rc(AnalysisResults(), AIReview(errors=["empty AI response"])), 2)

    def test_an_ai_failure_alone_is_exit_0_when_the_repo_opts_out(self):
        self.assertEqual(self._rc(AnalysisResults(), AIReview(errors=["empty"], ai_errors_block=False)), 0)

    def test_a_static_tool_error_still_exits_2_even_when_ai_errors_are_non_blocking(self):
        rc = self._rc(AnalysisResults(errors=["bandit: not found"]), AIReview(ai_errors_block=False))
        self.assertEqual(rc, 2)

    def test_a_static_block_still_exits_1_when_the_ai_review_failed_and_is_non_blocking(self):
        from qa_agent.static_analysis import Finding
        static = AnalysisResults(findings=[Finding(tool="bandit", severity="CRITICAL", category="security",
                                                   file="a.py", line=1, message="m")])
        self.assertEqual(self._rc(static, AIReview(errors=["empty"], ai_errors_block=False)), 1)

    def test_a_non_security_ai_high_is_advisory_exit_0_under_security_scope(self):
        ai = AIReview(findings=[_f("HIGH", "bug")], ai_blocking=True, ai_blocking_scope="security")
        self.assertEqual(self._rc(AnalysisResults(), ai), 0)

    def test_comment_says_ai_unavailable_advisory_instead_of_errored(self):
        body = pr_reporter._build_comment(AnalysisResults(), AIReview(errors=["empty"], ai_errors_block=False), "")
        self.assertIn("AI review unavailable", body)
        self.assertNotIn("ERRORED", body)
        self.assertIn("empty", body)  # the failure is still listed under Tool Warnings

    def test_commit_status_does_not_claim_a_clean_pass_when_ai_was_unavailable(self):
        calls = []
        with mock.patch.object(pr_reporter, "set_commit_status", side_effect=lambda **kw: calls.append(kw) or True):
            self._rc(AnalysisResults(), AIReview(errors=["empty"], ai_errors_block=False))
        self.assertTrue(calls[-1]["ai_unavailable"])

    def test_commit_status_description_for_the_advisory_case(self):
        posted = {}

        class R:
            status_code = 201
            text = ""

        def fake_post(url, **kw):
            posted.update(kw["json"])
            return R()

        with mock.patch.object(pr_reporter.config, "GITHUB_REPOSITORY", "o/r"), \
             mock.patch.object(pr_reporter.config, "GITHUB_SHA", "abc"), \
             mock.patch.object(pr_reporter, "_request_with_retry", lambda f, url, **kw: fake_post(url, **kw)):
            _REAL_SET_COMMIT_STATUS(blocked=False, errored=False, ai_unavailable=True)
        self.assertEqual(posted["state"], "success")
        self.assertIn("AI review unavailable", posted["description"])

    def test_a_clean_run_status_is_unchanged(self):
        calls = []
        with mock.patch.object(pr_reporter, "set_commit_status", side_effect=lambda **kw: calls.append(kw) or True):
            self._rc(AnalysisResults(), AIReview())
        self.assertFalse(calls[-1]["ai_unavailable"])

    def test_comment_still_says_errored_by_default(self):
        body = pr_reporter._build_comment(AnalysisResults(), AIReview(errors=["empty"]), "")
        self.assertIn("ERRORED", body)


if __name__ == "__main__":
    unittest.main()
