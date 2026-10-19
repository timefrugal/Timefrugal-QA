"""
Tests for the bounded-reasoning rescue (QA_AI_RESCUE_REASONING_EFFORT): when the QA_FALLBACK_MODEL spends its whole
token budget thinking and returns nothing usable, it is re-asked ONCE with reasoning limited, so a review ends with an
answer after a fixed amount of thinking. Off unless the env var is set.
"""
import io
import json
import unittest
from contextlib import redirect_stderr
from unittest import mock

from qa_agent import ai_review, config
from qa_agent.static_analysis import AnalysisResults

GOOD = json.dumps({"summary": "Looks fine.", "architecture_notes": "", "findings": []})


def _resp(content, reasoning=""):
    class _Msg:
        model_extra = {"reasoning": reasoning} if reasoning else {}
    _Msg.content = content

    class _Choice:
        message = _Msg()
        finish_reason = "length"

    class _Resp:
        choices = [_Choice()]
        usage = None
    return _Resp()


class _Script:
    """A client whose successive create() calls return the queued responses, recording every call's kwargs."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        outer = self

        class _Completions:
            def create(self, **kwargs):
                outer.calls.append(kwargs)
                return outer.responses.pop(0)

        class _Chat:
            completions = _Completions()
        self.chat = _Chat()


def _review(script, model="fb-model", effort="none", fallback_model="fb-model"):
    def fake_call_with_fallback(make_request):
        return make_request(script, model), "fallback", model

    err = io.StringIO()
    with mock.patch.object(ai_review, "_call_with_fallback", fake_call_with_fallback), \
         mock.patch.object(config, "QA_FALLBACK_MODEL", fallback_model), \
         mock.patch.object(config, "AI_RESCUE_REASONING_EFFORT", effort), \
         mock.patch.object(config, "AI_RESCUE_MAX_TOKENS", 2500), \
         mock.patch.object(config, "AI_MAX_TOKENS", 8000), \
         redirect_stderr(err):
        review = ai_review.review_code({"app.py": "print('hi')"}, AnalysisResults(), repo_name="r", language="python")
    return review, err.getvalue()


class TestRescue(unittest.TestCase):
    def test_an_empty_reply_is_retried_once_with_reasoning_limited_and_a_small_cap(self):
        script = _Script([_resp("", reasoning="thinking " * 50), _resp(GOOD)])
        review, _ = _review(script)
        self.assertEqual(len(script.calls), 2)
        first, second = script.calls
        self.assertEqual(first["max_tokens"], 8000)
        self.assertEqual(second["max_tokens"], 2500)
        self.assertEqual(second["extra_body"], {"reasoning_effort": "none"})
        self.assertEqual(first["messages"], second["messages"])  # same prompt, nothing dropped
        self.assertEqual(review.errors, [])

    def test_a_rescued_review_is_labelled_reduced_depth(self):
        review, _ = _review(_Script([_resp(""), _resp(GOOD)]))
        self.assertTrue(review.summary.startswith("(Reduced-depth review"))
        self.assertIn("Looks fine.", review.summary)

    def test_a_normal_review_makes_one_call_and_is_not_labelled(self):
        script = _Script([_resp(GOOD)])
        review, _ = _review(script)
        self.assertEqual(len(script.calls), 1)
        self.assertEqual(review.summary, "Looks fine.")

    def test_it_is_off_unless_the_env_var_is_set(self):
        script = _Script([_resp("", reasoning="x" * 40)])
        review, _ = _review(script, effort="")
        self.assertEqual(len(script.calls), 1)
        self.assertTrue(any("valid JSON" in e for e in review.errors))

    def test_it_only_applies_to_the_fallback_model(self):
        script = _Script([_resp("")])
        review, _ = _review(script, model="groq-model", fallback_model="fb-model")
        self.assertEqual(len(script.calls), 1)
        self.assertTrue(review.errors)

    def test_it_retries_only_once_when_the_rescue_is_empty_too(self):
        script = _Script([_resp(""), _resp("", reasoning="still thinking")])
        review, log = _review(script)
        self.assertEqual(len(script.calls), 2)
        self.assertTrue(any("valid JSON" in e for e in review.errors))
        self.assertIn("rescue attempt also empty", log)

    def test_json_salvaged_from_the_first_reply_reasoning_skips_the_rescue(self):
        script = _Script([_resp("", reasoning="analysis... " + GOOD)])
        review, _ = _review(script)
        self.assertEqual(len(script.calls), 1)
        self.assertEqual(review.errors, [])
        self.assertFalse(review.summary.startswith("(Reduced-depth"))

    def test_json_salvaged_from_the_rescue_reasoning_is_accepted(self):
        script = _Script([_resp(""), _resp("", reasoning="thoughts " + GOOD)])
        review, _ = _review(script)
        self.assertEqual(review.errors, [])
        self.assertTrue(review.summary.startswith("(Reduced-depth"))


class TestConfigDefaults(unittest.TestCase):
    def test_rescue_is_off_by_default_with_a_small_cap(self):
        self.assertEqual(config.AI_RESCUE_REASONING_EFFORT, "")
        self.assertLess(config.AI_RESCUE_MAX_TOKENS, config.AI_MAX_TOKENS + 5000)


if __name__ == "__main__":
    unittest.main()
