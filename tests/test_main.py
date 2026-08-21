"""
Tests for qa_agent.__main__'s argument handling -- the first test coverage
this CLI entry point has had.

Regression coverage for issue #19: `--model` was a complete silent no-op.
It set `os.environ["QA_AI_MODEL"]`, but `qa_agent.config` reads every
QA_AI_MODEL* env var at module-IMPORT time into module-level constants, and
`config` is already fully imported by the time argparse runs (transitively,
via `__main__.py`'s top-level `from qa_agent.agent import run`) -- so the
env var write landed after the values it was meant to influence were
already bound.

`run()` must be mocked at the `qa_agent.__main__` binding, not
`qa_agent.agent.run` -- `__main__.py` imports it via
`from qa_agent.agent import run`, which binds a local name in `__main__`'s
own namespace at import time. Patching `qa_agent.agent.run` only rebinds
the name in `agent`'s namespace; `__main__.main()` already holds its own
reference and would call straight through to the (unmocked) real
implementation, silently making live AI-provider calls. Patching
`qa_agent.__main__.run` (the module attribute `main()` actually looks up)
is the only patch site that actually intercepts the call.

`config.AI_PROVIDERS` is substituted with a hand-built fake list (matching
tests/test_ai_review.py's `mock.patch.object(config, "AI_PROVIDERS", ...)`
pattern) rather than mutating the real one, so these tests don't depend on
or clobber real provider config/env state.

Uses stdlib unittest (no pytest / test framework is set up in this repo
yet), following the convention established in tests/test_repo_config.py.
"""
import contextlib
import io
import os
import sys
import unittest
from unittest import mock

from qa_agent import __main__ as qa_main
from qa_agent import ai_review, config


def _fake_providers():
    """A fresh 4-entry fake provider list per call, so tests that mutate
    it (exactly what the --model override does) never share mutable state
    with each other."""
    return [
        {"name": "groq", "base_url": "https://groq.example/v1", "api_key": "groq-key", "model": "groq-default-model"},
        {"name": "cerebras", "base_url": "https://cerebras.example/v1", "api_key": "cerebras-key", "model": "cerebras-default-model"},
        {"name": "mistral", "base_url": "https://mistral.example/v1", "api_key": "mistral-key", "model": "mistral-default-model"},
        {"name": "fallback", "base_url": "", "api_key": "", "model": "", "env_key": "QA_FALLBACK_API_KEY"},
    ]


class TestModelFlagOverride(unittest.TestCase):
    """--model must reach every provider in the chain, since the docs
    promise it overrides the model for "whichever provider ends up
    handling the request" -- and the request can land on any tier of the
    Groq -> Cerebras -> Mistral -> fallback chain, not just the first."""

    def setUp(self):
        self.providers = _fake_providers()
        patchers = [
            mock.patch.object(config, "AI_PROVIDERS", self.providers),
            mock.patch.object(config, "AI_MODEL", "groq-default-model"),
            mock.patch.object(config, "QA_FALLBACK_MODEL", "fallback-sentinel-model"),
            mock.patch.dict(os.environ, {}, clear=False),
        ]
        for p in patchers:
            p.start()
            self.addCleanup(p.stop)
        run_patcher = mock.patch.object(qa_main, "run", return_value=0)
        self.fake_run = run_patcher.start()
        self.addCleanup(run_patcher.stop)

    def _run_main(self, argv):
        with mock.patch.object(sys, "argv", ["qa_agent"] + argv):
            with contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit) as ctx:
                    qa_main.main()
        return ctx.exception.code

    def test_model_flag_reaches_every_provider_in_the_chain(self):
        """The core fix: without the in-place mutation in __main__.py,
        config.AI_PROVIDERS still holds each provider's original default
        model even after --model is passed, because config was already
        imported (and its constants already bound) before argparse ran."""
        self._run_main(["--model", "override-model-xyz"])
        self.assertEqual(
            [p["model"] for p in self.providers],
            ["override-model-xyz"] * 4,
        )
        self.assertEqual(self.fake_run.call_count, 1)

    def test_model_flag_updates_config_ai_model_constant(self):
        """config.AI_MODEL (the constant that seeds the Groq entry) must
        stay in sync with the override so it never silently disagrees with
        the model actually being used."""
        self._run_main(["--model", "override-model-xyz"])
        self.assertEqual(config.AI_MODEL, "override-model-xyz")

    def test_model_flag_still_sets_the_env_var(self):
        """The env var write is kept even though it isn't what makes
        --model work in this process -- it's the documented knob, and it's
        what a child process or a later importlib.reload(config) would
        read."""
        self._run_main(["--model", "override-model-xyz"])
        self.assertEqual(os.environ["QA_AI_MODEL"], "override-model-xyz")

    def test_override_does_not_configure_the_unconfigured_fallback_slot(self):
        """Setting `model` on the fallback entry must stay harmless when
        its api_key/base_url are still empty -- _configured_providers
        requires api_key AND base_url AND model all non-empty, so the
        fallback slot must still be skipped exactly as before the
        override."""
        self._run_main(["--model", "override-model-xyz"])
        configured_names = [p["name"] for p in ai_review._configured_providers()]
        self.assertEqual(configured_names, ["groq", "cerebras", "mistral"])

    def test_override_does_not_rewrite_the_fallback_identity_marker(self):
        """config.QA_FALLBACK_MODEL must be left alone: ai_review compares
        `model == config.QA_FALLBACK_MODEL` as an identity marker to scope
        the fallback-only think:false extra_body and JSON-schema
        response_format. Rewriting it to the override value would make
        every provider match that check and send fallback-only params to
        Groq/Cerebras/Mistral, which their strict schema validation can
        reject."""
        self._run_main(["--model", "override-model-xyz"])
        self.assertEqual(config.QA_FALLBACK_MODEL, "fallback-sentinel-model")

    def test_no_model_flag_leaves_the_chain_untouched(self):
        """Omitting --model must be a complete no-op for the provider
        chain, config.AI_MODEL, and the env var -- confirming the override
        code path only activates when args.model is actually set."""
        env_before = os.environ.get("QA_AI_MODEL")
        self._run_main([])
        self.assertEqual(
            [p["model"] for p in self.providers],
            ["groq-default-model", "cerebras-default-model", "mistral-default-model", ""],
        )
        self.assertEqual(config.AI_MODEL, "groq-default-model")
        self.assertEqual(os.environ.get("QA_AI_MODEL"), env_before)


if __name__ == "__main__":
    unittest.main()
