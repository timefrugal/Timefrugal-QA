"""
Hardening of the bounded-reasoning rescue after an independent review: the rescue must not multiply the first call under
the outer 5xx/429 retry loop, must also cover an answer cut off mid-JSON, must reject a bare/shapeless rescue reply, must
label only the review that really came from the rescue, must work when streamed, must send the same model/messages/
response_format on both calls, and reduced-depth reviews must say so in the comment header and commit status.
"""
import io
import json
import unittest
from contextlib import redirect_stderr
from types import SimpleNamespace
from unittest import mock

from qa_agent import agent, ai_review, config, pr_reporter
from qa_agent.ai_review import AIReview
from qa_agent.static_analysis import AnalysisResults
from tests.test_ai_review import _fake_api_error

import openai

GOOD = json.dumps({"summary": "Looks fine.", "architecture_notes": "", "findings": []})
_REAL_SET_COMMIT_STATUS = pr_reporter.set_commit_status


def _resp(content, reasoning="", finish="length"):
    msg = SimpleNamespace(content=content, model_extra={"reasoning": reasoning} if reasoning else {})
    return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason=finish)], usage=None)


class _Client:
    """create() pops scripted items: a response, or an exception to raise. Records every call's kwargs."""

    def __init__(self, script):
        self.script, self.calls = list(script), []
        outer = self

        class _C:
            def create(self, **kwargs):
                outer.calls.append(kwargs)
                item = outer.script.pop(0)
                if isinstance(item, Exception):
                    raise item
                return item
        self.chat = SimpleNamespace(completions=_C())


def _run(clients_by_provider, providers, fallback_model="m", effort="none", stream=False):
    """review_code through the REAL _call_with_fallback/_call_with_retry with fake OpenAI clients."""
    it = iter(clients_by_provider)
    err = io.StringIO()
    with mock.patch.object(config, "AI_PROVIDERS", providers), \
         mock.patch.object(ai_review, "OpenAI", lambda **kw: next(it)), \
         mock.patch.object(ai_review.time, "sleep", lambda s: None), \
         mock.patch.object(config, "QA_FALLBACK_MODEL", fallback_model), \
         mock.patch.object(config, "AI_RESCUE_REASONING_EFFORT", effort), \
         mock.patch.object(config, "AI_RESCUE_MAX_TOKENS", 2500), \
         mock.patch.object(config, "AI_MAX_TOKENS", 8000), \
         mock.patch.object(config, "AI_STREAM", stream), \
         mock.patch.object(config, "AI_RETRY_MAX_ATTEMPTS", 3), \
         mock.patch.object(config, "AI_FALLBACK_RETRY_MAX_ATTEMPTS", 5), \
         redirect_stderr(err):
        review = ai_review.review_code({"app.py": "print('hi')"}, AnalysisResults(), repo_name="r", language="python")
    return review, err.getvalue()


def _prov(name, model="m"):
    return {"name": name, "base_url": f"https://{name}.example/v1", "api_key": "k", "model": model}


class TestRescueDoesNotMultiplyTheFirstCall(unittest.TestCase):
    def test_a_503_on_the_rescue_call_retries_only_the_rescue(self):
        c = _Client([_resp(""), _fake_api_error(openai.InternalServerError, {"retry_after_seconds": 5}),
                     _resp(GOOD, finish="stop")])
        review, _ = _run([c], [_prov("fallback")])
        self.assertEqual([k["max_tokens"] for k in c.calls], [8000, 2500, 2500])  # NOT [8000, 2500, 8000, 2500]
        self.assertEqual(review.errors, [])
        self.assertTrue(review.reduced_depth)

    def test_a_503_on_the_first_call_still_repeats_the_first_call(self):
        c = _Client([_fake_api_error(openai.InternalServerError, {"retry_after_seconds": 5}), _resp(GOOD, finish="stop")])
        review, _ = _run([c], [_prov("fallback")])
        self.assertEqual([k["max_tokens"] for k in c.calls], [8000, 8000])
        self.assertFalse(review.reduced_depth)


class TestWhatTriggersTheRescue(unittest.TestCase):
    def test_an_answer_cut_off_mid_json_at_the_length_limit_is_rescued(self):
        c = _Client([_resp('{"summary": "Mostly fine", "findings": [{"severity": "HI', finish="length"),
                     _resp(GOOD, finish="stop")])
        review, _ = _run([c], [_prov("fallback")])
        self.assertEqual(len(c.calls), 2)
        self.assertTrue(review.reduced_depth)

    def test_a_complete_answer_at_the_length_limit_is_not_rescued(self):
        c = _Client([_resp(GOOD, finish="length")])
        review, _ = _run([c], [_prov("fallback")])
        self.assertEqual(len(c.calls), 1)
        self.assertFalse(review.reduced_depth)

    def test_truncated_json_with_a_normal_stop_is_not_rescued(self):
        c = _Client([_resp('{"summary": "x"', finish="stop")])
        review, _ = _run([c], [_prov("fallback")])
        self.assertEqual(len(c.calls), 1)
        self.assertTrue(any("valid JSON" in e for e in review.errors))


class TestRescueReplyMustHaveTheReviewShape(unittest.TestCase):
    def _rescued(self, content):
        c = _Client([_resp(""), _resp(content, finish="stop")])
        return _run([c], [_prov("fallback")])[0]

    def test_a_bare_empty_object_is_not_a_clean_pass(self):
        r = self._rescued("{}")
        self.assertTrue(any("valid JSON" in e for e in r.errors))
        self.assertFalse(r.reduced_depth)

    def test_a_summary_without_a_findings_list_is_rejected(self):
        self.assertTrue(self._rescued('{"summary": "fine"}').errors)
        self.assertTrue(self._rescued('{"summary": "fine", "findings": "none"}').errors)

    def test_a_proper_empty_review_is_accepted_and_labelled(self):
        r = self._rescued(GOOD)
        self.assertEqual(r.errors, [])
        self.assertTrue(r.reduced_depth)
        self.assertTrue(r.summary.startswith("(Reduced-depth review"))


class TestLabelBelongsToTheReviewThatCameFromTheRescue(unittest.TestCase):
    def test_a_later_provider_with_the_same_model_name_is_not_mislabelled(self):
        first = _Client([_resp(""), _resp("not json at all", finish="stop")])  # rescue fails to parse -> next provider
        second = _Client([_resp(GOOD, finish="stop")])
        review, _ = _run([first, second], [_prov("cerebras"), _prov("fallback")])
        self.assertEqual(review.provider, "fallback")
        self.assertFalse(review.reduced_depth)
        self.assertEqual(review.summary, "Looks fine.")


class TestBothCallsAgree(unittest.TestCase):
    def test_model_messages_and_response_format_are_identical_only_budget_and_reasoning_differ(self):
        c = _Client([_resp(""), _resp(GOOD, finish="stop")])
        _run([c], [_prov("fallback")])
        a, b = c.calls
        self.assertEqual(a["model"], b["model"])
        self.assertEqual(a["messages"], b["messages"])
        self.assertEqual(repr(a["response_format"]), repr(b["response_format"]))
        self.assertEqual(b["extra_body"], {"reasoning_effort": "none"})
        self.assertEqual((a["max_tokens"], b["max_tokens"]), (8000, 2500))


class TestStreaming(unittest.TestCase):
    def _stream(self, content, finish):
        def chunk(c, fin=None):
            return SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=c, model_extra={}),
                                                            finish_reason=fin)])
        parts = [chunk(content[i:i + 20]) for i in range(0, len(content), 20)] or [chunk("")]
        return iter(parts + [chunk(None, finish)])

    def test_the_rescue_works_when_the_replies_are_streamed(self):
        c = _Client([self._stream("", "length"), self._stream(GOOD, "stop")])
        review, _ = _run([c], [_prov("fallback")], stream=True)
        self.assertEqual(len(c.calls), 2)
        self.assertTrue(all(k.get("stream") for k in c.calls))
        self.assertEqual(review.errors, [])
        self.assertTrue(review.reduced_depth)


class TestDiagnosticsAndReporting(unittest.TestCase):
    def test_the_empty_rescue_log_line_reports_the_rescue_budget(self):
        c = _Client([_resp(""), _resp("", finish="length")])
        _, log = _run([c], [_prov("fallback")])
        self.assertIn("rescue attempt also empty", log)
        self.assertIn("max_tokens=2500", log.split("rescue attempt also empty")[1])

    def test_comment_header_says_reduced_depth(self):
        body = pr_reporter._build_comment(AnalysisResults(), AIReview(reduced_depth=True), "")
        self.assertIn("reduced depth", body)
        self.assertNotIn("All checks passed", body)

    def test_a_normal_run_header_is_unchanged(self):
        self.assertIn("All checks passed", pr_reporter._build_comment(AnalysisResults(), AIReview(), ""))

    def test_commit_status_description_says_reduced_depth(self):
        posted = {}

        class R:
            status_code = 201
            text = ""

        with mock.patch.object(pr_reporter.config, "GITHUB_REPOSITORY", "o/r"), \
             mock.patch.object(pr_reporter.config, "GITHUB_SHA", "abc"), \
             mock.patch.object(pr_reporter, "_request_with_retry",
                               lambda f, url, **kw: posted.update(kw["json"]) or R()):
            _REAL_SET_COMMIT_STATUS(blocked=False, errored=False, ai_reduced_depth=True)
        self.assertEqual(posted["state"], "success")
        self.assertIn("reduced depth", posted["description"])

    def test_the_agent_passes_the_flag_to_the_commit_status(self):
        calls = []
        with mock.patch.object(agent, "get_changed_files", return_value=["a.py"]), \
             mock.patch.object(agent, "read_file_contents", return_value={"a.py": "x=1\n"}), \
             mock.patch.object(pr_reporter, "post_pr_comment", return_value=True), \
             mock.patch.object(pr_reporter, "write_step_summary", return_value=None), \
             mock.patch.object(pr_reporter, "set_commit_status", side_effect=lambda **kw: calls.append(kw) or True), \
             mock.patch.object(agent, "run_all", return_value=AnalysisResults()), \
             mock.patch.object(agent, "review_code", return_value=AIReview(reduced_depth=True)), \
             mock.patch.object(agent, "generate_tests", return_value=""):
            agent.run(mode="ci", pr_number="1", project_root=".", generate_test_cases=False)
        self.assertTrue(calls[-1]["ai_reduced_depth"])


if __name__ == "__main__":
    unittest.main()
