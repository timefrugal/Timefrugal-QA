"""
Second independent review of the bounded-reasoning rescue (v1.3.4.post8) -> post9. Pins: a draft found in cut-off
reasoning must not pass as a full-depth review; strict repos (ai.errors_block: true) must not get a clean pass from a
shallow rescue; a null/blank summary or non-dict findings must not crash or pass; a complete review wrapped in prose at
the limit is used; config parsing is forgiving but never turns the rescue on by accident.
"""
import io
import json
import unittest
from contextlib import redirect_stderr
from types import SimpleNamespace
from unittest import mock

from qa_agent import agent, ai_review, config, pr_reporter
from qa_agent.ai_review import AIReview
from qa_agent.repo_config import RepoConfig
from qa_agent.static_analysis import AnalysisResults
from tests.test_rescue_hardening import _Client, _prov, _resp, _run

GOOD = json.dumps({"summary": "Looks fine.", "architecture_notes": "", "findings": []})
WITH_FINDING = json.dumps({"summary": "One issue.", "architecture_notes": "", "findings": [
    {"severity": "HIGH", "category": "security", "file": "app.py", "line": 12, "message": "SQL injection",
     "suggestion": "parametrise"}]})
DRAFT_THEN_CUT = ('draft {"summary":"No issues found.","architecture_notes":"","findings":[]} '
                  'Wait, line 12 ... SQL injection, HIGH ... Let me re')


class TestDraftInCutOffReasoning(unittest.TestCase):
    def test_a_draft_in_reasoning_at_the_length_limit_triggers_the_rescue_instead_of_passing(self):
        c = _Client([_resp("", reasoning=DRAFT_THEN_CUT, finish="length"), _resp(WITH_FINDING, finish="stop")])
        review, _ = _run([c], [_prov("fallback")])
        self.assertEqual(len(c.calls), 2)
        self.assertEqual([f.severity for f in review.findings], ["HIGH"])
        self.assertTrue(review.reduced_depth)

    def test_if_the_rescue_also_fails_the_draft_is_the_last_resort_and_is_labelled_reduced_depth(self):
        c = _Client([_resp("", reasoning=DRAFT_THEN_CUT, finish="length"), _resp("", finish="length")])
        review, log = _run([c], [_prov("fallback")])
        self.assertEqual(len(c.calls), 2)
        self.assertTrue(review.reduced_depth)
        self.assertIn("using the draft review", log)

    def test_a_draft_in_reasoning_at_a_NORMAL_stop_is_still_accepted_without_a_rescue(self):
        c = _Client([_resp("", reasoning="analysis " + GOOD, finish="stop")])
        review, _ = _run([c], [_prov("fallback")])
        self.assertEqual(len(c.calls), 1)
        self.assertFalse(review.reduced_depth)

    def test_with_the_rescue_off_the_old_salvage_behaviour_is_unchanged(self):
        c = _Client([_resp("", reasoning=DRAFT_THEN_CUT, finish="length")])
        review, _ = _run([c], [_prov("fallback")], effort="")
        self.assertEqual(len(c.calls), 1)
        self.assertFalse(review.reduced_depth)

    def test_a_complete_review_wrapped_in_prose_and_cut_off_after_it_is_used_not_thrown_away(self):
        c = _Client([_resp("Here is my review:\n" + WITH_FINDING + "\nLet me also add", finish="length")])
        review, _ = _run([c], [_prov("fallback")])
        self.assertEqual(len(c.calls), 1)
        self.assertEqual([f.severity for f in review.findings], ["HIGH"])
        self.assertFalse(review.reduced_depth)


class TestStrictReposDoNotGetACleanPassFromAShallowRescue(unittest.TestCase):
    def _strict(self, rescue_reply):
        c = _Client([_resp(""), _resp(rescue_reply, finish="stop")])
        return _run([c], [_prov("fallback")], errors_block=True)[0]

    def test_errors_block_true_and_nothing_found_is_an_ai_failure(self):
        review = self._strict(GOOD)
        self.assertTrue(review.reduced_depth)
        self.assertTrue(review.blocking_errors)
        self.assertIn("reduced depth", review.errors[-1])

    def test_errors_block_true_but_a_finding_was_reported_is_not_an_error(self):
        review = self._strict(WITH_FINDING)
        self.assertEqual(review.errors, [])
        self.assertEqual(len(review.findings), 1)

    def test_errors_block_false_nothing_found_stays_an_advisory_pass(self):
        c = _Client([_resp(""), _resp(GOOD, finish="stop")])
        review, _ = _run([c], [_prov("fallback")], errors_block=False)
        self.assertEqual(review.errors, [])
        self.assertTrue(review.reduced_depth)

    def test_the_exit_code_for_a_strict_repo_is_2_not_0(self):
        review = self._strict(GOOD)
        with mock.patch.object(agent, "get_changed_files", return_value=["a.py"]), \
                mock.patch.object(agent, "read_file_contents", return_value={"a.py": "x=1\n"}), \
                mock.patch.object(pr_reporter, "post_pr_comment", return_value=True), \
                mock.patch.object(pr_reporter, "write_step_summary", return_value=None), \
                mock.patch.object(pr_reporter, "set_commit_status", return_value=True), \
                mock.patch.object(agent, "run_all", return_value=AnalysisResults()), \
                mock.patch.object(agent, "review_code", return_value=review), \
                mock.patch.object(agent, "generate_tests", return_value=""):
            rc = agent.run(mode="ci", pr_number="1", project_root=".", generate_test_cases=False)
        self.assertEqual(rc, 2)


class TestMalformedRescueReplies(unittest.TestCase):
    def _rescued(self, content):
        c = _Client([_resp(""), _resp(content, finish="stop")])
        return _run([c], [_prov("fallback")])[0]

    def test_null_blank_or_non_string_summary_is_rejected_not_a_crash(self):
        for summary in (None, "", "   ", ["x"], 7):
            r = self._rescued(json.dumps({"summary": summary, "findings": []}))
            self.assertTrue(any("valid JSON" in e for e in r.errors), summary)
            self.assertFalse(r.reduced_depth)

    def test_non_dict_findings_items_are_skipped_not_a_crash(self):
        c = _Client([_resp(json.dumps({"summary": "s", "architecture_notes": "", "findings": ["junk", 3, None, {
            "severity": "LOW", "category": "bug", "file": "a.py", "line": 1, "message": "m", "suggestion": "s"}]}),
            finish="stop")])
        review, _ = _run([c], [_prov("fallback")], effort="")
        self.assertEqual(len(review.findings), 1)

    def test_a_non_string_summary_on_a_normal_reply_does_not_crash(self):
        c = _Client([_resp(json.dumps({"summary": None, "architecture_notes": "", "findings": []}), finish="stop")])
        review, _ = _run([c], [_prov("fallback")], effort="")
        self.assertEqual(review.summary, "")


class TestRescueConfigParsing(unittest.TestCase):
    def effort(self, raw):
        with mock.patch.dict("os.environ", {"QA_AI_RESCUE_REASONING_EFFORT": raw}), redirect_stderr(io.StringIO()):
            return config._rescue_effort()

    def tokens(self, raw):
        with mock.patch.dict("os.environ", {"QA_AI_RESCUE_MAX_TOKENS": raw}), redirect_stderr(io.StringIO()):
            return config._rescue_max_tokens()

    def test_effort_is_case_insensitive_and_only_known_values_enable_it(self):
        for raw, want in (("none", "none"), ("None", "none"), (" NONE ", "none"), ("low", "low"), ("High", "high")):
            self.assertEqual(self.effort(raw), want, raw)

    def test_off_words_and_typos_leave_the_rescue_off(self):
        for raw in ("", "false", "False", "off", "0", "no", "nonee", "disabled", "true", "on"):
            self.assertEqual(self.effort(raw), "", raw)

    def test_off_words_switch_it_off_silently_without_a_warning(self):
        for raw in ("", "false", "False", "off", "0", "no"):
            err = io.StringIO()
            with mock.patch.dict("os.environ", {"QA_AI_RESCUE_REASONING_EFFORT": raw}), redirect_stderr(err):
                self.assertEqual(config._rescue_effort(), "")
            self.assertEqual(err.getvalue(), "", raw)

    def test_an_unknown_effort_warns(self):
        err = io.StringIO()
        with mock.patch.dict("os.environ", {"QA_AI_RESCUE_REASONING_EFFORT": "nonee"}), redirect_stderr(err):
            config._rescue_effort()
        self.assertIn("rescue stays OFF", err.getvalue())

    def test_token_budget_defaults_validates_and_is_capped(self):
        self.assertEqual(self.tokens(""), 2500)
        self.assertEqual(self.tokens("1800"), 1800)
        for raw in ("abc", "0", "-5", "2.5k", "2500.0"):
            self.assertEqual(self.tokens(raw), 2500, raw)
        self.assertEqual(self.tokens("100000"), 8000)

    def test_the_module_level_values_come_from_those_functions(self):
        # (the module attributes are evaluated at import from the same functions; pinned so a refactor cannot split them)
        import inspect
        src = inspect.getsource(config)
        self.assertIn("AI_RESCUE_REASONING_EFFORT = _rescue_effort()", src)
        self.assertIn("AI_RESCUE_MAX_TOKENS = _rescue_max_tokens()", src)


if __name__ == "__main__":
    unittest.main()
