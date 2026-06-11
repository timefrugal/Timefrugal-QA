#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# run_local_qa.sh
#
# Run Timefrugal-QA locally BEFORE raising a GitHub PR.
# This catches issues early, avoids multiple CI iterations, and saves
# GitHub Actions minutes.
#
# Usage (from your project directory):
#   export GITHUB_TOKEN=ghp_yourtoken
#   bash /path/to/Timefrugal-QA/scripts/run_local_qa.sh
#
#   # Or with options:
#   bash /path/to/Timefrugal-QA/scripts/run_local_qa.sh --base develop --no-tests
#
# Options:
#   --base <branch>   Base branch to diff against (default: main)
#   --no-tests        Skip AI test generation (faster)
#   --model <name>    Override AI model (default: gpt-4o-mini)
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

BASE_REF="main"
NO_TESTS=""
MODEL=""

# ── Parse args ───────────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case $1 in
    --base)   BASE_REF="$2"; shift 2 ;;
    --no-tests) NO_TESTS="--no-tests"; shift ;;
    --model)  MODEL="--model $2"; shift 2 ;;
    *) echo "Unknown option: $1"; exit 1 ;;
  esac
done

# ── Check GITHUB_TOKEN ───────────────────────────────────────────────────────
if [ -z "${GITHUB_TOKEN:-}" ]; then
  # Try gh CLI token as fallback
  if command -v gh &>/dev/null && gh auth status &>/dev/null 2>&1; then
    export GITHUB_TOKEN=$(gh auth token)
    echo "ℹ️  Using GitHub CLI token"
  else
    echo "❌ GITHUB_TOKEN not set. Required for GitHub Models AI."
    echo "   Either: export GITHUB_TOKEN=ghp_yourtoken"
    echo "   Or:     gh auth login  (then re-run)"
    exit 1
  fi
fi

# ── Check we're in a git repo ────────────────────────────────────────────────
if ! git rev-parse --git-dir &>/dev/null; then
  echo "❌ Not inside a git repository. Run from your project root."
  exit 1
fi

# ── Install QA agent if not present ─────────────────────────────────────────
if ! python -m qa_agent --help &>/dev/null 2>&1; then
  echo "📦 Installing Timefrugal-QA agent..."
  pip install git+https://github.com/Timefrugal/Timefrugal-QA.git@main --quiet
  pip install bandit semgrep pylint mypy radon pip-audit --quiet
fi

# ── Run the agent ────────────────────────────────────────────────────────────
echo ""
python -m qa_agent --base "origin/$BASE_REF" $NO_TESTS $MODEL
EXIT_CODE=$?

if [ $EXIT_CODE -eq 0 ]; then
  echo ""
  echo "✅ QA passed — you can safely raise a PR."
else
  echo ""
  echo "❌ QA found blocking issues — fix them before raising a PR."
  echo "   See qa_report.md for details."
fi

exit $EXIT_CODE
