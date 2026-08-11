"""
Tests for qa_agent.ai_review.

Uses stdlib unittest (no pytest / test framework is set up in this repo yet),
following the convention established in tests/test_repo_config.py.
"""
import unittest
from unittest import mock

from qa_agent import ai_review, config
from qa_agent.ai_review import _get_review_prompt, _per_file_char_budget, _validate_severity
from qa_agent.repo_config import RepoConfig
from qa_agent.static_analysis import AnalysisResults


class TestValidateSeverityRejectsInvalidAndMissingValues(unittest.TestCase):
    """
    H1: the AI's JSON response is untrusted input -- a hallucinated, wrong-case,
    or missing severity string must never be trusted outright. _validate_severity
    is the single chokepoint that guarantees every AIFinding.severity is one of
    config.SEVERITY_ORDER, falling back to config.SEVERITY_INFO otherwise.
    """

    def test_valid_severity_passes_through_unchanged(self):
        self.assertEqual(_validate_severity("CRITICAL"), "CRITICAL")
        self.assertEqual(_validate_severity("HIGH"), "HIGH")
        self.assertEqual(_validate_severity("MEDIUM"), "MEDIUM")
        self.assertEqual(_validate_severity("LOW"), "LOW")
        self.assertEqual(_validate_severity("INFO"), "INFO")

    def test_lowercase_valid_value_is_normalized_to_uppercase(self):
        # The real implementation upper()s before checking membership, so it
        # is case-INsensitive -- a lowercase valid severity is accepted and
        # normalized, not rejected. Testing actual behavior, not an assumption.
        self.assertEqual(_validate_severity("high"), "HIGH")
        self.assertEqual(_validate_severity("Critical"), "CRITICAL")
        self.assertEqual(_validate_severity("mEdIuM"), "MEDIUM")

    def test_garbage_string_falls_back_to_info(self):
        self.assertEqual(_validate_severity("BOGUS"), config.SEVERITY_INFO)
        self.assertEqual(_validate_severity("super-duper-critical"), config.SEVERITY_INFO)

    def test_none_falls_back_to_info(self):
        self.assertEqual(_validate_severity(None), config.SEVERITY_INFO)

    def test_empty_string_falls_back_to_info(self):
        self.assertEqual(_validate_severity(""), config.SEVERITY_INFO)

    def test_whitespace_only_string_falls_back_to_info(self):
        self.assertEqual(_validate_severity("   "), config.SEVERITY_INFO)

    def test_surrounding_whitespace_is_stripped_on_valid_value(self):
        self.assertEqual(_validate_severity("  HIGH  "), "HIGH")


class TestGetReviewPromptAppendsExtraInstructions(unittest.TestCase):
    """
    extra_instructions is an opt-in per-repo addition to the AI review's
    system prompt (e.g. a repo that auto-deploys to a live production VM
    asking the reviewer to weigh outage risk). It must be clearly delimited
    and appended, not silently merged into the base prompt, and must leave
    the base prompt byte-for-byte unchanged when absent -- so repos that
    never opt in see zero behavior change.
    """

    def test_empty_extra_instructions_leaves_prompt_unchanged(self):
        self.assertEqual(_get_review_prompt("python"), _get_review_prompt("python", ""))

    def test_default_argument_matches_explicit_empty_string(self):
        for language in ("python", "java", "html"):
            self.assertEqual(_get_review_prompt(language), _get_review_prompt(language, ""))

    def test_non_empty_extra_instructions_is_appended(self):
        prompt = _get_review_prompt("python", "Weigh production-outage risk heavily.")
        self.assertIn("Weigh production-outage risk heavily.", prompt)
        # Still contains the full base prompt -- this is an addition, not a
        # replacement.
        self.assertIn("senior software engineer", prompt)

    def test_extra_instructions_section_is_clearly_delimited(self):
        prompt = _get_review_prompt("python", "Weigh production-outage risk heavily.")
        self.assertIn(
            "Additional repo-specific review focus (from this repo's "
            ".timefrugal-qa.yml):",
            prompt,
        )

    def test_unknown_language_still_appends_to_python_fallback_prompt(self):
        prompt = _get_review_prompt("cobol", "Some guidance.")
        self.assertIn("Some guidance.", prompt)
        self.assertIn("senior software engineer", prompt)  # fell back to python prompt


class TestPerFileCharBudgetKeepsTotalPromptSizeBounded(unittest.TestCase):
    """
    Regression coverage: a fixed per-file truncation cap scales unboundedly
    with file count -- a 9-file PR requested ~15,500 tokens against Groq's
    free-tier 12,000 TPM limit and got a 413. _per_file_char_budget spreads
    a total character budget (config.AI_MAX_TOTAL_CONTENT_CHARS) across
    however many files changed, so total prompt size stays roughly bounded
    regardless of file count.
    """

    def test_few_files_keeps_original_per_file_cap(self):
        # 1-2 files: well under the total budget, so the original fixed cap
        # (matches prior single-file behavior) should win, not the split.
        with mock.patch.object(config, "AI_MAX_TOTAL_CONTENT_CHARS", 16000):
            self.assertEqual(_per_file_char_budget(1, per_file_cap=6000), 6000)
            self.assertEqual(_per_file_char_budget(2, per_file_cap=6000), 6000)

    def test_many_files_splits_budget_and_stays_bounded(self):
        with mock.patch.object(config, "AI_MAX_TOTAL_CONTENT_CHARS", 16000):
            budget = _per_file_char_budget(9, per_file_cap=6000)
            self.assertLess(budget, 6000)
            # Total across all files must not exceed the configured budget.
            self.assertLessEqual(budget * 9, 16000)

    def test_very_many_files_never_goes_below_floor(self):
        with mock.patch.object(config, "AI_MAX_TOTAL_CONTENT_CHARS", 16000):
            budget = _per_file_char_budget(500, per_file_cap=6000, floor=500)
            self.assertEqual(budget, 500)

    def test_zero_files_returns_the_cap_unchanged(self):
        # Defensive: review_code/generate_tests both bail out earlier on
        # empty file_contents, but this must not raise (division by zero).
        self.assertEqual(_per_file_char_budget(0, per_file_cap=6000), 6000)


def _fake_provider(name, api_key="key-set", model=None):
    return {
        "name": name,
        "base_url": f"https://{name}.example/v1",
        "api_key": api_key,
        "model": model or f"{name}-model",
    }


class TestConfiguredProvidersFiltersByApiKeyPresence(unittest.TestCase):
    def test_providers_without_api_key_are_excluded(self):
        providers = [
            _fake_provider("groq"),
            _fake_provider("cerebras", api_key=""),
            _fake_provider("mistral"),
        ]
        with mock.patch.object(config, "AI_PROVIDERS", providers):
            configured = ai_review._configured_providers()
        self.assertEqual([p["name"] for p in configured], ["groq", "mistral"])

    def test_all_missing_keys_returns_empty_list(self):
        providers = [_fake_provider("groq", api_key=""), _fake_provider("cerebras", api_key="")]
        with mock.patch.object(config, "AI_PROVIDERS", providers):
            self.assertEqual(ai_review._configured_providers(), [])


class TestCallWithFallback(unittest.TestCase):
    """
    Regression coverage: a single free-tier provider running out of quota
    (Groq's 12K TPM, hit by this very PR's diff) must not take the whole AI
    review down when another configured provider could serve the request.
    """

    def test_first_provider_success_does_not_touch_the_next(self):
        providers = [_fake_provider("groq"), _fake_provider("cerebras")]
        calls = []

        def make_request(client, model):
            calls.append(model)
            return "ok"

        with mock.patch.object(config, "AI_PROVIDERS", providers):
            result = ai_review._call_with_fallback(make_request)

        self.assertEqual(result, "ok")
        self.assertEqual(calls, ["groq-model"])  # cerebras never invoked

    def test_falls_back_to_next_provider_on_failure(self):
        providers = [_fake_provider("groq"), _fake_provider("cerebras")]
        calls = []

        def make_request(client, model):
            calls.append(model)
            if model == "groq-model":
                raise RuntimeError("groq out of quota")
            return "cerebras result"

        with mock.patch.object(config, "AI_PROVIDERS", providers):
            result = ai_review._call_with_fallback(make_request)

        self.assertEqual(result, "cerebras result")
        self.assertEqual(calls, ["groq-model", "cerebras-model"])

    def test_falls_through_an_unconfigured_middle_provider(self):
        providers = [
            _fake_provider("groq"),
            _fake_provider("cerebras", api_key=""),  # no key -- must be skipped, not tried
            _fake_provider("mistral"),
        ]
        calls = []

        def make_request(client, model):
            calls.append(model)
            if model == "groq-model":
                raise RuntimeError("groq out of quota")
            return "mistral result"

        with mock.patch.object(config, "AI_PROVIDERS", providers):
            result = ai_review._call_with_fallback(make_request)

        self.assertEqual(result, "mistral result")
        self.assertEqual(calls, ["groq-model", "mistral-model"])  # cerebras skipped entirely

    def test_raises_last_providers_error_when_all_fail(self):
        providers = [_fake_provider("groq"), _fake_provider("cerebras")]

        def make_request(client, model):
            if model == "groq-model":
                raise RuntimeError("groq failure")
            raise RuntimeError("cerebras failure")

        with mock.patch.object(config, "AI_PROVIDERS", providers):
            with self.assertRaises(RuntimeError) as ctx:
                ai_review._call_with_fallback(make_request)

        self.assertEqual(str(ctx.exception), "cerebras failure")

    def test_raises_clear_error_when_no_providers_configured(self):
        providers = [_fake_provider("groq", api_key=""), _fake_provider("cerebras", api_key="")]

        with mock.patch.object(config, "AI_PROVIDERS", providers):
            with self.assertRaises(ValueError) as ctx:
                ai_review._call_with_fallback(lambda client, model: "unreachable")

        self.assertIn("GROQ_API_KEY", str(ctx.exception))
        self.assertIn("CEREBRAS_API_KEY", str(ctx.exception))


class TestReviewCodePassesRepoConfigExtraInstructionsIntoSystemPrompt(unittest.TestCase):
    """
    review_code() must thread repo_config.extra_instructions through to the
    system prompt actually sent to the AI provider -- not just to
    _get_review_prompt() in isolation. Captures the real `messages` kwarg
    passed to the (stubbed) provider call.
    """

    def _capture_system_prompt(self, repo_config):
        captured = {}

        class _FakeMessage:
            content = '{"summary": "ok", "findings": []}'

        class _FakeChoice:
            message = _FakeMessage()

        class _FakeResponse:
            choices = [_FakeChoice()]

        class _FakeCompletions:
            def create(self, **kwargs):
                captured["messages"] = kwargs["messages"]
                return _FakeResponse()

        class _FakeChat:
            completions = _FakeCompletions()

        class _FakeClient:
            chat = _FakeChat()

        def fake_call_with_fallback(make_request):
            return make_request(_FakeClient(), "fake-model")

        with mock.patch.object(ai_review, "_call_with_fallback", fake_call_with_fallback):
            ai_review.review_code(
                {"app.py": "print('hi')"},
                AnalysisResults(),
                repo_name="test-repo",
                language="python",
                repo_config=repo_config,
            )
        return captured["messages"][0]["content"]

    def test_extra_instructions_present_in_sent_system_prompt(self):
        repo_config = RepoConfig(extra_instructions="Weigh production-outage risk heavily.")
        system_prompt = self._capture_system_prompt(repo_config)
        self.assertIn("Weigh production-outage risk heavily.", system_prompt)

    def test_repo_config_none_leaves_system_prompt_unchanged(self):
        with_none = self._capture_system_prompt(None)
        with_default = self._capture_system_prompt(RepoConfig())
        self.assertEqual(with_none, with_default)
        self.assertEqual(with_none, _get_review_prompt("python"))

    def test_repo_config_with_empty_extra_instructions_leaves_prompt_unchanged(self):
        system_prompt = self._capture_system_prompt(RepoConfig(extra_instructions=""))
        self.assertEqual(system_prompt, _get_review_prompt("python"))


def _fake_openai_client_factory(content_by_model: dict, calls: list):
    """Builds a fake replacement for ai_review.OpenAI: `OpenAI(base_url=...,
    api_key=...)` returns a client whose `.chat.completions.create(**kwargs)`
    looks up `kwargs["model"]` in content_by_model and returns a response
    object shaped like the real openai SDK's
    (`response.choices[0].message.content`). Every call is recorded (by
    model name) into `calls`, so tests can assert exactly which providers
    were actually invoked -- not just what the final result was."""

    class _FakeMessage:
        def __init__(self, content):
            self.content = content

    class _FakeChoice:
        def __init__(self, content):
            self.message = _FakeMessage(content)

    class _FakeResponse:
        def __init__(self, content):
            self.choices = [_FakeChoice(content)]

    class _FakeCompletions:
        def create(self, **kwargs):
            model = kwargs["model"]
            calls.append(model)
            return _FakeResponse(content_by_model[model])

    class _FakeChat:
        completions = _FakeCompletions()

    class _FakeClient:
        chat = _FakeChat()

    def _factory(base_url=None, api_key=None):  # pylint: disable=unused-argument
        return _FakeClient()

    return _factory


class TestGarbageJsonFromEarlierProviderFallsThroughToNextProvider(unittest.TestCase):
    """
    Regression test for the 2026-08-11 incident (jarvis-infra issue #200):
    Groq's org-wide daily quota was exhausted, Cerebras (the next provider
    in the chain) responded HTTP 200 with content that didn't parse as
    JSON, and the QA gate crashed outright instead of falling through to
    Mistral -- because JSON parsing happened AFTER _call_with_fallback
    returned, so a garbage-but-200 response counted as "success" at the
    transport level and the chain never advanced.

    This test goes through the REAL review_code() -> _call_with_fallback()
    -> _parse_review_json() pipeline (only the OpenAI client itself is
    faked, at the class ai_review.OpenAI is constructed from) -- it does
    NOT re-implement or stub out the fallback/parsing logic under test, so
    it actually exercises the fix rather than assuming it.

    Mutation check performed manually while writing this test: reverting
    the fix (moving JSON parsing back to after `_call_with_fallback`
    returns, matching the pre-fix code) makes this test fail -- the first
    (garbage) provider's response is accepted as a "successful" call, the
    chain never reaches the second provider, `calls` ends up
    `["groq-model"]` instead of `["groq-model", "cerebras-model"]`, and
    review.errors is non-empty instead of empty.
    """

    def test_garbage_200_response_advances_to_next_provider(self):
        providers = [_fake_provider("groq"), _fake_provider("cerebras")]
        calls: list = []
        content_by_model = {
            "groq-model": "this is not JSON at all {{{ garbage",
            "cerebras-model": '{"summary": "ok from cerebras", "architecture_notes": "", "findings": []}',
        }

        with mock.patch.object(config, "AI_PROVIDERS", providers), \
                mock.patch.object(ai_review, "OpenAI", _fake_openai_client_factory(content_by_model, calls)):
            review = ai_review.review_code(
                {"app.py": "print('hi')"},
                AnalysisResults(),
                repo_name="test-repo",
                language="python",
            )

        # Both providers were actually tried, in order -- proves real
        # fallthrough happened, not just a lucky first-provider result.
        self.assertEqual(calls, ["groq-model", "cerebras-model"])
        self.assertEqual(review.errors, [])
        self.assertEqual(review.summary, "ok from cerebras")

    def test_garbage_200_from_every_provider_still_reports_a_clear_error(self):
        # When even the LAST provider's response fails to parse, review_code
        # must still fail closed with a clear error (not crash) -- this is
        # the "all providers exhausted" edge of the same fix.
        providers = [_fake_provider("groq"), _fake_provider("cerebras")]
        calls: list = []
        content_by_model = {
            "groq-model": "garbage one",
            "cerebras-model": "garbage two",
        }

        with mock.patch.object(config, "AI_PROVIDERS", providers), \
                mock.patch.object(ai_review, "OpenAI", _fake_openai_client_factory(content_by_model, calls)):
            review = ai_review.review_code(
                {"app.py": "print('hi')"},
                AnalysisResults(),
                repo_name="test-repo",
                language="python",
            )

        self.assertEqual(calls, ["groq-model", "cerebras-model"])
        self.assertEqual(len(review.errors), 1)
        self.assertIn("valid JSON", review.errors[0])

    def test_fourth_fallback_provider_used_only_after_first_three_fail(self):
        # End-to-end shape check with all four real provider names (groq,
        # cerebras, mistral, fallback) -- mirrors config.AI_PROVIDERS'
        # actual order, proving the new last-resort entry is reachable
        # (and is genuinely last) through the real pipeline, not just in
        # the config list itself (see tests/test_config.py for that).
        providers = [
            _fake_provider("groq"),
            _fake_provider("cerebras"),
            _fake_provider("mistral"),
            _fake_provider("fallback", model="z13-model"),
        ]
        calls: list = []
        content_by_model = {
            "groq-model": "garbage",
            "cerebras-model": "also garbage",
            "mistral-model": "still garbage",
            "z13-model": '{"summary": "ok from z13 fallback", "architecture_notes": "", "findings": []}',
        }

        with mock.patch.object(config, "AI_PROVIDERS", providers), \
                mock.patch.object(ai_review, "OpenAI", _fake_openai_client_factory(content_by_model, calls)):
            review = ai_review.review_code(
                {"app.py": "print('hi')"},
                AnalysisResults(),
                repo_name="test-repo",
                language="python",
            )

        self.assertEqual(calls, ["groq-model", "cerebras-model", "mistral-model", "z13-model"])
        self.assertEqual(review.errors, [])
        self.assertEqual(review.summary, "ok from z13 fallback")


class TestFourthProviderConfiguredOrSkipped(unittest.TestCase):
    """H1-style regression coverage for the new 4th ("fallback") provider
    entry's chain behavior, using the same fake-provider-list pattern as
    TestCallWithFallback above -- complements test_config.py's coverage of
    the real env-var-driven config.AI_PROVIDERS construction."""

    def test_fallback_provider_skipped_when_no_api_key(self):
        providers = [
            _fake_provider("groq"),
            _fake_provider("cerebras"),
            _fake_provider("mistral"),
            _fake_provider("fallback", api_key=""),  # not configured
        ]
        with mock.patch.object(config, "AI_PROVIDERS", providers):
            configured = ai_review._configured_providers()
        self.assertEqual([p["name"] for p in configured], ["groq", "cerebras", "mistral"])

    def test_fallback_provider_only_reached_after_the_first_three_fail(self):
        providers = [
            _fake_provider("groq"),
            _fake_provider("cerebras"),
            _fake_provider("mistral"),
            _fake_provider("fallback"),
        ]
        calls = []

        def make_request(client, model):
            calls.append(model)
            if model == "fallback-model":
                return "z13 result"
            raise RuntimeError(f"{model} failed")

        with mock.patch.object(config, "AI_PROVIDERS", providers):
            result = ai_review._call_with_fallback(make_request)

        self.assertEqual(result, "z13 result")
        self.assertEqual(calls, ["groq-model", "cerebras-model", "mistral-model", "fallback-model"])

    def test_fallback_provider_never_tried_if_an_earlier_provider_succeeds(self):
        providers = [
            _fake_provider("groq"),
            _fake_provider("cerebras"),
            _fake_provider("mistral"),
            _fake_provider("fallback"),
        ]
        calls = []

        def make_request(client, model):
            calls.append(model)
            return "groq result"

        with mock.patch.object(config, "AI_PROVIDERS", providers):
            result = ai_review._call_with_fallback(make_request)

        self.assertEqual(result, "groq result")
        self.assertEqual(calls, ["groq-model"])  # cerebras/mistral/fallback never invoked


if __name__ == "__main__":
    unittest.main()
