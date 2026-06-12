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

# Windows PowerShell defaults to cp1252; reconfigure to UTF-8 so rich can render emoji
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

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
        help="Override GitHub Models AI model (default: gpt-4o-mini)",
    )

    args = parser.parse_args()

    # Allow model override via CLI flag
    if args.model:
        os.environ["QA_AI_MODEL"] = args.model

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
