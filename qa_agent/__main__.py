"""
CLI entry point — run as:
    python -m qa_agent               # local mode (before raising a PR)
    python -m qa_agent --ci          # CI mode (inside GitHub Actions)
    python -m qa_agent --base main   # diff against 'main' branch
    python -m qa_agent --no-tests    # skip test generation
"""
import argparse
import os
import sys

# Force line-buffered stdout/stderr so progress prints stream in real time
# under CI (stdout is fully block-buffered when not a TTY, which otherwise
# makes every [agent] print silently accumulate and dump in one burst right
# as the process exits -- see jarvis-infra issue #286).
try:
    sys.stdout.reconfigure(line_buffering=True)  # type: ignore[union-attr]
    sys.stderr.reconfigure(line_buffering=True)  # type: ignore[union-attr]
    # Windows PowerShell defaults to cp1252; reconfigure to UTF-8 so rich can render emoji.
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from qa_agent import config
from qa_agent.agent import run


def main():
    parser = argparse.ArgumentParser(
        prog="qa_agent",
        description="Timefrugal-QA — AI-powered code review and testing agent",
    )
    parser.add_argument(
        "--ci",
        action="store_true",
        help="Run in CI mode (posts GitHub PR comment + sets commit status)",
    )
    parser.add_argument(
        "--base",
        default=None,
        help="Git base ref to diff against (default: origin/main)",
    )
    parser.add_argument(
        "--pr",
        default=None,
        help="Pull request number (CI mode only)",
    )
    parser.add_argument(
        "--root",
        default=".",
        help="Project root directory (default: current directory)",
    )
    parser.add_argument(
        "--no-tests",
        action="store_true",
        help="Skip AI test case generation",
    )
    parser.add_argument(
        "--commit-tests",
        action="store_true",
        help="Write generated tests to tests/ and commit them (local mode only)",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Override the AI review model (default: openai/gpt-oss-120b)",
    )

    args = parser.parse_args()

    # Allow model override via CLI flag.
    #
    # Setting QA_AI_MODEL alone does NOT work (issue #19): config.py reads
    # every QA_AI_MODEL* env var at module-IMPORT time into module-level
    # constants, and qa_agent.config is already imported by the time
    # argparse runs (transitively, via `from qa_agent.agent import run`
    # above) -- so this assignment landed after the values it was meant to
    # influence were already bound, and --model was a silent no-op for as
    # long as the flag has existed. The env var is still set: it's the
    # documented knob, it's what any child process or later
    # importlib.reload of config would read, and it keeps `--model` and
    # `QA_AI_MODEL=...` describing the same state.
    #
    # What actually takes effect in THIS process is the in-place mutation
    # below. It works because ai_review reads `config.AI_PROVIDERS` as a
    # module attribute at call time (_configured_providers /
    # _call_with_fallback), never as an import-time snapshot. It's applied
    # to EVERY entry, not just Groq, because --model is documented
    # (README, CLAUDE.md) as overriding the model for "whichever provider
    # ends up handling the request" -- and the request can land on any tier
    # of the fallback chain. Setting `model` on an otherwise-unconfigured
    # entry (e.g. the generic fallback slot with no QA_FALLBACK_API_KEY /
    # QA_FALLBACK_BASE_URL) stays harmless: _configured_providers requires
    # api_key AND base_url AND model all non-empty, so that slot is still
    # skipped exactly as before.
    #
    # config.QA_FALLBACK_MODEL is deliberately NOT rewritten. ai_review
    # uses it as an identity marker (`model == config.QA_FALLBACK_MODEL`)
    # to scope the fallback-only think:false extra_body and JSON-schema
    # response_format; pointing it at the override value would make every
    # provider match that check and send fallback-only params to
    # Groq/Cerebras/Mistral, which their strict schema validation can
    # reject. Consequence, accepted: under --model the fallback tier runs
    # without that special-casing, since the tuning belongs to the
    # configured fallback model, not to a model the caller substituted.
    if args.model:
        os.environ["QA_AI_MODEL"] = args.model
        # Kept in sync with the provider entry it seeds (config.py:125) so
        # config.AI_MODEL never silently disagrees with the model actually
        # being used.
        config.AI_MODEL = args.model
        for provider in config.AI_PROVIDERS:
            provider["model"] = args.model

    # Determine base ref
    base_ref = args.base or ("origin/main" if not args.ci else os.getenv("GITHUB_BASE_REF", "origin/main"))

    mode = "ci" if args.ci else "local"
    pr_number = args.pr or os.getenv("PR_NUMBER", "")

    if mode == "local":
        print("=" * 60)
        print("  Timefrugal-QA — Local Pre-PR Check")
        print("=" * 60)

    exit_code = run(
        mode=mode,
        base_ref=base_ref,
        pr_number=pr_number,
        project_root=args.root,
        generate_test_cases=not args.no_tests,
        commit_tests=args.commit_tests,
    )
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
