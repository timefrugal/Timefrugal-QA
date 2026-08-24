"""
Tests for qa_agent.ai_review.

Uses stdlib unittest (no pytest / test framework is set up in this repo yet),
following the convention established in tests/test_repo_config.py.
"""
import unittest
from unittest import mock

import json

import openai

from qa_agent import ai_review, config
from qa_agent.ai_review import (
    AIFinding,
    _demote_if_outside_diff,
    _get_review_prompt,
    _parse_review_json,
    _per_file_char_budget,
    _validate_severity,
)
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


def _fake_provider(name, api_key="key-set", model=None, base_url=None):
    return {
        "name": name,
        "base_url": base_url if base_url is not None else f"https://{name}.example/v1",
        "api_key": api_key,
        "model": model if model is not None else f"{name}-model",
    }


class TestParseReviewJsonRejectsGarbageEmptyAndNonObjectContent(unittest.TestCase):
    """
    Independent review (before this shipped) found the original version of
    this fix still had two live crash/false-pass paths one layer deeper
    than the garbage-JSON case:

    - Non-dict-but-valid JSON (a bare list/string/number all parse fine
      via json.loads) used to be returned as-is, so review_code()'s later
      `data.get("summary", "")` would raise AttributeError OUTSIDE the
      try/except that's supposed to catch provider failures -- crashing
      review_code entirely instead of falling through to the next
      provider. Exactly the bug class this whole PR exists to close, one
      level further in.
    - Empty/whitespace-only content used to default to "{}" (a *valid*,
      clean, zero-findings review) instead of being treated as a failure
      -- a silent false pass, worse than a crash, and the Z13 fallback
      provider's own known failure mode (a model burning its whole token
      budget on hidden reasoning with empty visible output).
    """

    def test_valid_object_json_parses_normally(self):
        data = _parse_review_json('{"summary": "ok", "findings": []}')
        self.assertEqual(data, {"summary": "ok", "findings": []})

    def test_markdown_fenced_valid_object_json_parses_normally(self):
        data = _parse_review_json('```json\n{"summary": "ok", "findings": []}\n```')
        self.assertEqual(data, {"summary": "ok", "findings": []})

    def test_json_array_raises_instead_of_returning_non_dict(self):
        with self.assertRaises(json.JSONDecodeError):
            _parse_review_json("[1, 2, 3]")

    def test_json_string_raises_instead_of_returning_non_dict(self):
        with self.assertRaises(json.JSONDecodeError):
            _parse_review_json('"just a string"')

    def test_json_number_raises_instead_of_returning_non_dict(self):
        with self.assertRaises(json.JSONDecodeError):
            _parse_review_json("42")

    def test_none_content_raises_instead_of_defaulting_to_empty_object(self):
        with self.assertRaises(json.JSONDecodeError):
            _parse_review_json(None)

    def test_empty_string_content_raises_instead_of_defaulting_to_empty_object(self):
        with self.assertRaises(json.JSONDecodeError):
            _parse_review_json("")

    def test_whitespace_only_content_raises(self):
        with self.assertRaises(json.JSONDecodeError):
            _parse_review_json("   \n  ")

    def test_empty_content_inside_markdown_fence_raises(self):
        with self.assertRaises(json.JSONDecodeError):
            _parse_review_json("```json\n\n```")

    def test_plain_garbage_still_raises(self):
        with self.assertRaises(json.JSONDecodeError):
            _parse_review_json("not json at all {{{")


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


class TestConfiguredProvidersRequiresBaseUrlAndModelTooForGenericSlots(unittest.TestCase):
    """
    Independent review found that _configured_providers originally gated
    ONLY on api_key -- fine for Groq/Cerebras/Mistral (their base_url and
    model always come from a real hardcoded/defaulted value, never
    empty), but wrong for the generic QA_FALLBACK_* slot, which has no
    default base_url or model. An operator who set QA_FALLBACK_API_KEY
    without also setting QA_FALLBACK_BASE_URL/QA_FALLBACK_MODEL would
    otherwise have that entry "count" as configured, then fail with a
    confusing error deep inside the openai SDK (empty base URL) instead
    of being cleanly skipped the same way a wholly-unconfigured provider
    is. _configured_providers now requires api_key AND base_url AND model
    all non-empty.
    """

    def test_api_key_alone_without_base_url_is_not_configured(self):
        providers = [_fake_provider("fallback", base_url="")]
        with mock.patch.object(config, "AI_PROVIDERS", providers):
            self.assertEqual(ai_review._configured_providers(), [])

    def test_api_key_alone_without_model_is_not_configured(self):
        providers = [_fake_provider("fallback", model="")]
        with mock.patch.object(config, "AI_PROVIDERS", providers):
            self.assertEqual(ai_review._configured_providers(), [])

    def test_all_three_fields_present_is_configured(self):
        providers = [_fake_provider("fallback", base_url="http://z13.example/v1", model="z13-model")]
        with mock.patch.object(config, "AI_PROVIDERS", providers):
            configured = ai_review._configured_providers()
        self.assertEqual([p["name"] for p in configured], ["fallback"])

    def test_this_check_is_a_no_op_for_the_three_named_cloud_providers(self):
        # Groq/Cerebras/Mistral's real config.py entries always have a
        # non-empty base_url and model (hardcoded or os.getenv-defaulted),
        # so this new requirement must not change their existing
        # api-key-only-gated behavior.
        providers = [_fake_provider("groq"), _fake_provider("cerebras"), _fake_provider("mistral")]
        with mock.patch.object(config, "AI_PROVIDERS", providers):
            configured = ai_review._configured_providers()
        self.assertEqual([p["name"] for p in configured], ["groq", "cerebras", "mistral"])


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


class TestReviewCodeScopesResponseFormatToFallbackModelOnly(unittest.TestCase):
    """
    _REVIEW_JSON_RESPONSE_SCHEMA (decoding-time JSON-schema enforcement) must
    be sent only when the provider being called IS QA_FALLBACK_MODEL, mirroring
    the identical extra_body={"think": False} scoping already in place --
    Groq/Cerebras/Mistral already comply with the plain-prompt schema in
    production, so they must never receive an unfamiliar response_format
    that could change their behavior for no benefit.

    Captures the real `response_format` kwarg passed to the (stubbed)
    provider call, going through the real review_code() -> _make_request
    path rather than re-implementing the scoping logic under test.
    """

    def _capture_response_format(self, model_name, fallback_model, response_format_enabled=True):
        captured = {}

        class _FakeMessage:
            content = '{"summary": "ok", "architecture_notes": "", "findings": []}'

        class _FakeChoice:
            message = _FakeMessage()

        class _FakeResponse:
            choices = [_FakeChoice()]

        class _FakeCompletions:
            def create(self, **kwargs):
                captured["response_format"] = kwargs.get("response_format")
                return _FakeResponse()

        class _FakeChat:
            completions = _FakeCompletions()

        class _FakeClient:
            chat = _FakeChat()

        def fake_call_with_fallback(make_request):
            return make_request(_FakeClient(), model_name)

        with mock.patch.object(config, "QA_FALLBACK_MODEL", fallback_model), \
                mock.patch.object(config, "QA_FALLBACK_RESPONSE_FORMAT", response_format_enabled), \
                mock.patch.object(ai_review, "_call_with_fallback", fake_call_with_fallback):
            ai_review.review_code(
                {"app.py": "print('hi')"},
                AnalysisResults(),
                repo_name="test-repo",
                language="python",
            )
        return captured["response_format"]

    def test_fallback_model_call_receives_the_schema(self):
        response_format = self._capture_response_format("z13-model", fallback_model="z13-model")
        self.assertEqual(response_format, ai_review._REVIEW_JSON_RESPONSE_SCHEMA)

    def test_cloud_provider_call_does_not_receive_the_schema(self):
        # response_format's SDK type doesn't accept None (unlike
        # extra_body) -- openai.omit is the correct "not set" sentinel.
        response_format = self._capture_response_format("groq-model", fallback_model="z13-model")
        self.assertIs(response_format, openai.omit)

    def test_no_fallback_configured_never_sends_the_schema(self):
        # QA_FALLBACK_MODEL defaults to "" (unset) -- a call for a model
        # that happens to be the empty string must not match by accident.
        response_format = self._capture_response_format("groq-model", fallback_model="")
        self.assertIs(response_format, openai.omit)

    def test_fallback_model_opted_out_of_response_format_does_not_receive_the_schema(self):
        # Mika#66/#68: a model plugged into QA_FALLBACK_MODEL that already
        # emits schema-compliant JSON unconstrained can be actively hurt by
        # the schema constraint (observed: an 8/8 clean model started
        # failing 4/4 on truncation once this was force-applied). Operators
        # must be able to opt a specific deployment out entirely.
        response_format = self._capture_response_format(
            "z13-model", fallback_model="z13-model", response_format_enabled=False,
        )
        self.assertIs(response_format, openai.omit)

    def test_response_format_flag_defaults_on(self):
        # config.QA_FALLBACK_RESPONSE_FORMAT itself (env-derived, not the
        # test's own mock default) must default to True so existing
        # deployments (e.g. qwen2.5:7b, the model this was built/verified
        # against) keep today's behavior without setting a new env var.
        self.assertTrue(config.QA_FALLBACK_RESPONSE_FORMAT)


class TestReviewCodeScopesReasoningEffortToFallbackModelOnly(unittest.TestCase):
    """
    QA_FALLBACK_REASONING_EFFORT, when set, must replace the default
    extra_body={"think": False} with extra_body={"reasoning_effort": <value>}
    -- and only for QA_FALLBACK_MODEL, same scoping as response_format above.
    Exists because think:false is not honored by every model in this slot
    over Ollama's OpenAI-compat layer (confirmed on qwen3.8:27b,
    Timefrugal-QA#23): it burns the whole token budget on hidden reasoning
    and returns empty content, while an explicit reasoning_effort produces
    real output.

    Captures the real `extra_body` kwarg passed to the (stubbed) provider
    call, going through the real review_code() -> _make_request path rather
    than re-implementing the scoping logic under test.
    """

    def _capture_extra_body(self, model_name, fallback_model, reasoning_effort=""):
        captured = {}

        class _FakeMessage:
            content = '{"summary": "ok", "architecture_notes": "", "findings": []}'

        class _FakeChoice:
            message = _FakeMessage()

        class _FakeResponse:
            choices = [_FakeChoice()]

        class _FakeCompletions:
            def create(self, **kwargs):
                captured["extra_body"] = kwargs.get("extra_body")
                return _FakeResponse()

        class _FakeChat:
            completions = _FakeCompletions()

        class _FakeClient:
            chat = _FakeChat()

        def fake_call_with_fallback(make_request):
            return make_request(_FakeClient(), model_name)

        with mock.patch.object(config, "QA_FALLBACK_MODEL", fallback_model), \
                mock.patch.object(config, "QA_FALLBACK_REASONING_EFFORT", reasoning_effort), \
                mock.patch.object(ai_review, "_call_with_fallback", fake_call_with_fallback):
            ai_review.review_code(
                {"app.py": "print('hi')"},
                AnalysisResults(),
                repo_name="test-repo",
                language="python",
            )
        return captured["extra_body"]

    def test_fallback_model_defaults_to_think_false(self):
        extra_body = self._capture_extra_body("z13-model", fallback_model="z13-model")
        self.assertEqual(extra_body, {"think": False})

    def test_fallback_model_with_reasoning_effort_set_sends_it_instead(self):
        extra_body = self._capture_extra_body(
            "z13-model", fallback_model="z13-model", reasoning_effort="low",
        )
        self.assertEqual(extra_body, {"reasoning_effort": "low"})

    def test_cloud_provider_call_receives_no_extra_body_even_with_reasoning_effort_set(self):
        extra_body = self._capture_extra_body(
            "groq-model", fallback_model="z13-model", reasoning_effort="low",
        )
        self.assertIsNone(extra_body)

    def test_reasoning_effort_flag_defaults_empty(self):
        # config.QA_FALLBACK_REASONING_EFFORT itself (env-derived, not the
        # test's own mock default) must default to "" so existing
        # deployments keep today's think:false behavior without setting a
        # new env var.
        self.assertEqual(config.QA_FALLBACK_REASONING_EFFORT, "")


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

    def test_valid_but_non_object_json_from_first_provider_does_not_crash_review_code(self):
        # Independent-review regression: a provider returning a bare JSON
        # array/string/number parses SUCCESSFULLY via json.loads, so this
        # must not be confused with the garbage/unparseable case above --
        # it exercises a different path (_parse_review_json's isinstance
        # check) and, before that fix, would have raised AttributeError
        # OUTSIDE review_code's try/except (data.get(...) on a list) and
        # crashed the whole review instead of falling through.
        providers = [_fake_provider("groq"), _fake_provider("cerebras")]
        calls: list = []
        content_by_model = {
            "groq-model": "[1, 2, 3]",  # valid JSON, not an object
            "cerebras-model": '{"summary": "ok from cerebras", "architecture_notes": "", "findings": []}',
        }

        with mock.patch.object(config, "AI_PROVIDERS", providers), \
                mock.patch.object(ai_review, "OpenAI", _fake_openai_client_factory(content_by_model, calls)):
            review = ai_review.review_code(  # must not raise
                {"app.py": "print('hi')"},
                AnalysisResults(),
                repo_name="test-repo",
                language="python",
            )

        self.assertEqual(calls, ["groq-model", "cerebras-model"])
        self.assertEqual(review.errors, [])
        self.assertEqual(review.summary, "ok from cerebras")

    def test_empty_content_from_first_provider_does_not_silently_pass_as_clean_review(self):
        # Independent-review regression: empty/whitespace-only content
        # used to default to "{}" (a valid, clean, zero-findings review)
        # instead of being treated as a failure -- a silent false pass,
        # worse than a crash, and specifically relevant to this PR's
        # fourth-provider target (Z13's known empty-output failure mode
        # on some models). Must fall through instead of reporting clean.
        providers = [_fake_provider("groq"), _fake_provider("cerebras")]
        calls: list = []
        content_by_model = {
            "groq-model": "",
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

        self.assertEqual(calls, ["groq-model", "cerebras-model"])
        self.assertEqual(review.errors, [])
        self.assertEqual(review.summary, "ok from cerebras")  # NOT an empty clean pass from groq


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


class TestDemoteIfOutsideDiff(unittest.TestCase):
    """
    jarvis-infra issues #306/#307/#310: real free-tier AI reviewers
    repeatedly fabricated or escalated CRITICAL/HIGH findings against code
    entirely outside the PR's actual diff. _demote_if_outside_diff() is
    the structural (non-prompt-dependent) guard closing that gap -- these
    tests exercise the function directly, in isolation from review_code()'s
    provider-chain machinery.
    """

    def _finding(self, severity="CRITICAL", file="app.py", line=10):
        return AIFinding(
            severity=severity, category="security", file=file, line=line,
            message="dangerous thing", suggestion="fix it",
        )

    def test_critical_finding_inside_diff_passes_through_unchanged(self):
        finding = self._finding(severity="CRITICAL", line=10)
        result = _demote_if_outside_diff(finding, {"app.py": {8, 9, 10, 11}})
        self.assertIs(result, finding)
        self.assertEqual(result.severity, "CRITICAL")

    def test_high_finding_inside_diff_passes_through_unchanged(self):
        finding = self._finding(severity="HIGH", line=10)
        result = _demote_if_outside_diff(finding, {"app.py": {10}})
        self.assertEqual(result.severity, "HIGH")

    def test_critical_finding_outside_diff_is_demoted_to_medium(self):
        finding = self._finding(severity="CRITICAL", line=500)
        result = _demote_if_outside_diff(finding, {"app.py": {8, 9, 10, 11}})
        self.assertEqual(result.severity, config.SEVERITY_MEDIUM)
        self.assertIn("demoted from CRITICAL", result.message)
        self.assertIn("outside this PR's diff", result.message)
        # Original content preserved, not discarded -- still a useful
        # suggestion, just no longer blocking.
        self.assertIn("dangerous thing", result.message)
        self.assertEqual(result.suggestion, "fix it")

    def test_finding_for_a_file_not_in_the_diff_at_all_is_demoted(self):
        finding = self._finding(severity="HIGH", file="unrelated.py", line=1)
        result = _demote_if_outside_diff(finding, {"app.py": {10}})
        self.assertEqual(result.severity, config.SEVERITY_MEDIUM)

    def test_line_zero_uncertain_finding_is_demoted_even_if_file_in_diff(self):
        # Per _REVIEW_JSON_SCHEMA's own instruction to the model ("use 0 if
        # uncertain") -- an unverifiable line number can't be trusted at
        # blocking severity either.
        finding = self._finding(severity="CRITICAL", line=0)
        result = _demote_if_outside_diff(finding, {"app.py": {8, 9, 10, 11}})
        self.assertEqual(result.severity, config.SEVERITY_MEDIUM)

    def test_line_zero_demoted_for_high_severity_too_not_just_critical(self):
        # Same code path handles both severities identically -- covered
        # explicitly rather than assumed from the CRITICAL case above.
        finding = self._finding(severity="HIGH", line=0)
        result = _demote_if_outside_diff(finding, {"app.py": {8, 9, 10, 11}})
        self.assertEqual(result.severity, config.SEVERITY_MEDIUM)

    def test_medium_and_below_findings_are_never_touched_even_outside_diff(self):
        for severity in ("MEDIUM", "LOW", "INFO"):
            finding = self._finding(severity=severity, line=999)
            result = _demote_if_outside_diff(finding, {"app.py": {10}})
            self.assertIs(result, finding)
            self.assertEqual(result.severity, severity)

    def test_none_changed_line_ranges_is_a_no_op_backward_compat(self):
        # An older/other caller of review_code() that hasn't been updated
        # to compute changed_line_ranges must see EXACTLY today's
        # behavior -- findings pass through unchecked, not mass-demoted.
        finding = self._finding(severity="CRITICAL", line=999)
        result = _demote_if_outside_diff(finding, None)
        self.assertIs(result, finding)

    def test_empty_range_for_a_file_present_in_the_dict_demotes(self):
        # The file IS in the diff (e.g. a rename or deletion-only change)
        # but genuinely touched zero lines -- any CRITICAL/HIGH claim
        # about it is unverifiable and gets demoted.
        finding = self._finding(severity="HIGH", file="renamed.py", line=5)
        result = _demote_if_outside_diff(finding, {"renamed.py": set()})
        self.assertEqual(result.severity, config.SEVERITY_MEDIUM)


class TestReviewCodeDiffScopingEndToEnd(unittest.TestCase):
    """
    Same class of regression test as
    TestGarbageJsonFromEarlierProviderFallsThroughToNextProvider above --
    goes through the REAL review_code() (diff-section prompt construction
    AND the post-parse demotion pass), only the OpenAI client itself is
    faked.
    """

    def test_ai_finding_outside_diff_is_demoted_in_the_real_pipeline(self):
        providers = [_fake_provider("groq")]
        calls: list = []
        content_by_model = {
            "groq-model": json.dumps({
                "summary": "reviewed",
                "architecture_notes": "",
                "findings": [
                    {"severity": "CRITICAL", "category": "security", "file": "app.py",
                     "line": 500, "message": "fabricated, far from the real diff",
                     "suggestion": "n/a"},
                    {"severity": "HIGH", "category": "bug", "file": "app.py",
                     "line": 10, "message": "a real issue in the new code",
                     "suggestion": "fix it"},
                ],
            }),
        }

        with mock.patch.object(config, "AI_PROVIDERS", providers), \
                mock.patch.object(ai_review, "OpenAI", _fake_openai_client_factory(content_by_model, calls)):
            review = ai_review.review_code(
                {"app.py": "line1\n" * 600},
                AnalysisResults(),
                repo_name="test-repo",
                language="python",
                diff_text="--- a/app.py\n+++ b/app.py\n@@ -9,0 +10 @@\n+new line\n",
                changed_line_ranges={"app.py": {10}},
            )

        self.assertEqual(len(review.findings), 2)
        outside, inside = review.findings
        self.assertEqual(outside.severity, config.SEVERITY_MEDIUM)
        self.assertIn("demoted from CRITICAL", outside.message)
        self.assertEqual(inside.severity, "HIGH")  # genuinely inside the diff, untouched

    def test_no_changed_line_ranges_passed_keeps_prior_behavior(self):
        # Confirms review_code() itself defaults changed_line_ranges to
        # None when a caller doesn't pass it -- existing callers (and
        # existing tests elsewhere in this file that call review_code()
        # without the new kwargs) see unchanged behavior.
        providers = [_fake_provider("groq")]
        calls: list = []
        content_by_model = {
            "groq-model": json.dumps({
                "summary": "reviewed",
                "architecture_notes": "",
                "findings": [
                    {"severity": "CRITICAL", "category": "security", "file": "app.py",
                     "line": 500, "message": "would be demoted if ranges were passed",
                     "suggestion": "n/a"},
                ],
            }),
        }

        with mock.patch.object(config, "AI_PROVIDERS", providers), \
                mock.patch.object(ai_review, "OpenAI", _fake_openai_client_factory(content_by_model, calls)):
            review = ai_review.review_code(
                {"app.py": "print('hi')"},
                AnalysisResults(),
                repo_name="test-repo",
                language="python",
            )

        self.assertEqual(review.findings[0].severity, "CRITICAL")


class TestDiffSectionInReviewPrompt(unittest.TestCase):
    """The diff text, when provided, must actually reach the user message
    sent to the provider -- and be cleanly absent (not an empty/broken
    section) when no diff_text is given (e.g. a brand-new untracked file,
    or a caller that hasn't been updated)."""

    def _capture_user_content(self, diff_text):
        providers = [_fake_provider("groq")]
        captured = {}

        class _FakeMessage:
            content = '{"summary": "ok", "architecture_notes": "", "findings": []}'

        class _FakeChoice:
            message = _FakeMessage()

        class _FakeResponse:
            choices = [_FakeChoice()]

        class _FakeCompletions:
            def create(self, **kwargs):
                captured["user_content"] = kwargs["messages"][1]["content"]
                return _FakeResponse()

        class _FakeChat:
            completions = _FakeCompletions()

        class _FakeClient:
            chat = _FakeChat()

        with mock.patch.object(config, "AI_PROVIDERS", providers), \
                mock.patch.object(ai_review, "OpenAI", lambda base_url=None, api_key=None: _FakeClient()):
            ai_review.review_code(
                {"app.py": "print('hi')"},
                AnalysisResults(),
                repo_name="test-repo",
                language="python",
                diff_text=diff_text,
            )
        return captured["user_content"]

    def test_diff_text_appears_in_the_sent_prompt(self):
        content = self._capture_user_content("--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-old\n+new\n")
        self.assertIn("## Diff (what this PR actually changed)", content)
        self.assertIn("+new", content)

    def test_empty_diff_text_omits_the_diff_section_entirely(self):
        content = self._capture_user_content("")
        self.assertNotIn("## Diff (what this PR actually changed)", content)

    def test_diff_text_longer_than_cap_is_truncated(self):
        huge_diff = "+line\n" * 5000  # far larger than AI_MAX_DIFF_CHARS
        with mock.patch.object(config, "AI_MAX_DIFF_CHARS", 100):
            content = self._capture_user_content(huge_diff)
        self.assertIn("[diff truncated]", content)

    def test_empty_diff_text_does_not_tell_the_model_everything_is_pre_existing(self):
        # Regression test for a real bug found in independent review before
        # merge: an earlier version of this fix hoisted the "code shown
        # here is PRE-EXISTING, cap at MEDIUM" instruction out of the
        # `if diff_text:` guard, so it applied unconditionally -- meaning
        # a caller with no diff (an old caller that hasn't been updated,
        # OR agent.py's own get_diff_text() falling back to "" when `git
        # diff` itself fails) got told every changed line was pre-existing
        # and should be capped at MEDIUM, with NO diff shown to justify
        # that claim -- silently encouraging under-reporting of genuinely
        # new CRITICAL/HIGH issues on exactly the path this fix is
        # supposed to leave untouched. Mutation check: re-hoisting that
        # instruction out of the `if diff_text:` block makes this fail.
        content = self._capture_user_content("")
        self.assertNotIn("PRE-EXISTING", content)
        self.assertNotIn("MEDIUM or lower", content)
        # Exact pre-fix wording, reproduced when there's no diff to reason from.
        self.assertIn("## Changed Files\n", content)
        self.assertIn("Please perform a thorough code review of the changed files above.", content)


if __name__ == "__main__":
    unittest.main()
