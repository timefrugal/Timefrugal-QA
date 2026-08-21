"""
Tests for qa_agent.config's AI_PROVIDERS construction -- specifically the
fourth, last-resort QA_FALLBACK_* provider entry added for jarvis-infra
issue #200 (Z13 as a last-resort AI review provider when Groq, Cerebras,
AND Mistral are all exhausted/down/unconfigured).

config.py builds AI_PROVIDERS from os.getenv(...) calls at IMPORT time, so
unlike qa_agent.ai_review's tests (which substitute a hand-built fake
provider list via mock.patch.object), these tests reload the actual module
under a patched environment to exercise the real env-var wiring -- the
thing the parallel jarvis-infra/Z13-connectivity track depends on matching
exactly (QA_FALLBACK_BASE_URL / QA_FALLBACK_API_KEY / QA_FALLBACK_MODEL).

Uses stdlib unittest (no pytest / test framework is set up in this repo
yet), following the convention established in tests/test_repo_config.py.
"""
import importlib
import os
import unittest
from unittest import mock

from qa_agent import config as config_module


def _reload_config_with_env(overrides: dict):
    """Reload qa_agent.config with the given env vars overridden (empty
    string clears a var for this module's purposes, since every QA_FALLBACK_*
    read uses os.getenv(key, "") -- there's no distinction here between
    "unset" and "set to empty string"). Returns the freshly reloaded module
    object (same object identity as qa_agent.config -- importlib.reload
    mutates in place, it doesn't create a new module object)."""
    with mock.patch.dict(os.environ, overrides, clear=False):
        return importlib.reload(config_module)


class TestFallbackProviderEnvWiring(unittest.TestCase):
    """The exact three env var names (QA_FALLBACK_BASE_URL,
    QA_FALLBACK_API_KEY, QA_FALLBACK_MODEL) and ordering (last, after
    mistral) that jarvis-infra's parallel Z13-connectivity track needs to
    match when it provisions the real repo secrets."""

    def tearDown(self):
        # Reload once more against whatever the ambient (real) environment
        # actually is, so a test that injected fake QA_FALLBACK_* values
        # doesn't leak state into tests that import qa_agent.config next
        # (module reload mutates the single shared module object in place).
        importlib.reload(config_module)

    def test_fallback_entry_is_last_in_ai_providers(self):
        reloaded = _reload_config_with_env({
            "QA_FALLBACK_BASE_URL": "http://z13.example/v1",
            "QA_FALLBACK_API_KEY": "z13-key",
            "QA_FALLBACK_MODEL": "gpt-oss-120b",
        })
        names = [p["name"] for p in reloaded.AI_PROVIDERS]
        self.assertEqual(names, ["groq", "cerebras", "mistral", "fallback"])

    def test_fallback_entry_reads_its_three_env_vars_by_exact_name(self):
        reloaded = _reload_config_with_env({
            "QA_FALLBACK_BASE_URL": "http://z13.example/v1",
            "QA_FALLBACK_API_KEY": "z13-key",
            "QA_FALLBACK_MODEL": "gpt-oss-120b",
        })
        fallback = reloaded.AI_PROVIDERS[-1]
        self.assertEqual(fallback["base_url"], "http://z13.example/v1")
        self.assertEqual(fallback["api_key"], "z13-key")
        self.assertEqual(fallback["model"], "gpt-oss-120b")

    def test_fallback_entry_has_empty_api_key_when_env_vars_unset(self):
        # Explicitly force-empty rather than relying on ambient environment
        # not having these set -- "" and "unset" are equivalent for every
        # os.getenv(key, "") read in config.py, so this exercises the same
        # code path a genuinely-unset var would.
        reloaded = _reload_config_with_env({
            "QA_FALLBACK_BASE_URL": "",
            "QA_FALLBACK_API_KEY": "",
            "QA_FALLBACK_MODEL": "",
        })
        fallback = reloaded.AI_PROVIDERS[-1]
        self.assertEqual(fallback["name"], "fallback")
        self.assertEqual(fallback["api_key"], "")

    def test_fallback_provider_silently_skipped_when_unconfigured(self):
        # This is the actual consumer-facing guarantee: importing
        # qa_agent.ai_review's _configured_providers against the real
        # config module, with the fallback env vars unset, must not
        # surface a 4th provider.
        from qa_agent import ai_review

        _reload_config_with_env({
            "QA_FALLBACK_BASE_URL": "",
            "QA_FALLBACK_API_KEY": "",
            "QA_FALLBACK_MODEL": "",
        })
        configured_names = [p["name"] for p in ai_review._configured_providers()]
        self.assertNotIn("fallback", configured_names)

    def test_fallback_provider_present_and_last_when_configured(self):
        from qa_agent import ai_review

        _reload_config_with_env({
            "QA_FALLBACK_BASE_URL": "http://z13.example/v1",
            "QA_FALLBACK_API_KEY": "z13-key",
            "QA_FALLBACK_MODEL": "gpt-oss-120b",
        })
        configured = ai_review._configured_providers()
        self.assertEqual(configured[-1]["name"], "fallback")

    def test_fallback_provider_with_only_api_key_set_is_not_configured(self):
        # Independent review before merge: _configured_providers requires
        # api_key AND base_url AND model all non-empty for this generic
        # slot (unlike Groq/Cerebras/Mistral, whose base_url/model always
        # come from a real default). QA_FALLBACK_API_KEY alone -- a
        # plausible partial-setup mistake -- must not "count" as
        # configured; it would otherwise fail confusingly deep inside the
        # openai SDK (empty base URL) instead of being cleanly skipped.
        from qa_agent import ai_review

        _reload_config_with_env({
            "QA_FALLBACK_BASE_URL": "",
            "QA_FALLBACK_API_KEY": "z13-key",
            "QA_FALLBACK_MODEL": "",
        })
        configured_names = [p["name"] for p in ai_review._configured_providers()]
        self.assertNotIn("fallback", configured_names)

    def test_missing_provider_error_message_names_the_real_env_var(self):
        # config.AI_PROVIDERS' generic "{name.upper()}_API_KEY" derivation
        # would produce the wrong string ("FALLBACK_API_KEY") for this
        # entry -- the "no providers configured" error message must name
        # the actual QA_FALLBACK_API_KEY var a human/DJ would need to set.
        from qa_agent import ai_review

        _reload_config_with_env({
            "GROQ_API_KEY": "",
            "CEREBRAS_API_KEY": "",
            "MISTRAL_API_KEY": "",
            "QA_FALLBACK_API_KEY": "",
        })
        with self.assertRaises(ValueError) as ctx:
            ai_review._call_with_fallback(lambda client, model: "unreachable")
        # If env_key weren't honored, the generic f"{name.upper()}_API_KEY"
        # derivation would produce "FALLBACK_API_KEY" (no "QA_" prefix) --
        # a plain substring containing "QA_FALLBACK_API_KEY" only appears
        # in the message when the explicit env_key override is respected.
        self.assertIn("QA_FALLBACK_API_KEY", str(ctx.exception))


class TestGroqDefaultModel(unittest.TestCase):
    """jarvis-infra issue #309: Groq retired its entire llama chat lineup,
    so the old llama-3.3-70b-versatile default 404'd on every call -- tier 1
    of the four-tier chain was silently dead and every real QA run started
    at Cerebras or Mistral. Locks in the replacement tag (note the "openai/"
    namespace prefix, which Groq requires and Cerebras does not) and the
    QA_AI_MODEL override path that feeds AI_PROVIDERS[0]."""

    def tearDown(self):
        importlib.reload(config_module)

    def test_groq_is_first_provider_and_uses_the_new_default_model(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("QA_AI_MODEL", None)
            reloaded = importlib.reload(config_module)
            self.assertEqual(reloaded.AI_MODEL, "openai/gpt-oss-120b")
            self.assertEqual(reloaded.AI_PROVIDERS[0]["name"], "groq")
            self.assertEqual(
                reloaded.AI_PROVIDERS[0]["model"], "openai/gpt-oss-120b"
            )

    def test_qa_ai_model_env_override_reaches_the_groq_provider_entry(self):
        reloaded = _reload_config_with_env({"QA_AI_MODEL": "some/other-model"})
        self.assertEqual(reloaded.AI_MODEL, "some/other-model")
        self.assertEqual(reloaded.AI_PROVIDERS[0]["model"], "some/other-model")


if __name__ == "__main__":
    unittest.main()
