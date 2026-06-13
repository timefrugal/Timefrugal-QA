#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# setup_new_repo.sh
#
# Adds the Timefrugal-QA workflow to a single newly-created repository.
# Requires: GitHub CLI (gh) — https://cli.github.com
#
# Usage:
#   bash scripts/setup_new_repo.sh <owner/repo>
#   bash scripts/setup_new_repo.sh my-new-project          # owner inferred from gh auth
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

WORKFLOW_SOURCE="$(dirname "$0")/../templates/repo_workflow.yml"
WORKFLOW_DEST=".github/workflows/qa.yml"
COMMIT_MSG="chore: add Timefrugal-QA automated review workflow"

# ── Preflight ────────────────────────────────────────────────────────────────
if ! command -v gh &>/dev/null; then
  echo "❌ GitHub CLI (gh) is not installed. Install from https://cli.github.com"
  exit 1
fi

if ! gh auth status &>/dev/null; then
  echo "❌ Not authenticated. Run: gh auth login"
  exit 1
fi

if [ $# -eq 0 ]; then
  echo "Usage: bash scripts/setup_new_repo.sh <owner/repo-name>"
  echo "       bash scripts/setup_new_repo.sh my-new-project"
  exit 1
fi

INPUT="$1"

# If no slash, prepend the authenticated user
if [[ "$INPUT" != */* ]]; then
  GH_USER=$(gh api user --jq '.login')
  FULL_REPO="$GH_USER/$INPUT"
else
  FULL_REPO="$INPUT"
fi

echo "Setting up Timefrugal-QA for: $FULL_REPO"

# ── Check if workflow already exists ─────────────────────────────────────────
if gh api "repos/$FULL_REPO/contents/$WORKFLOW_DEST" &>/dev/null 2>&1; then
  echo "⏭️  Workflow already exists at $WORKFLOW_DEST — nothing to do."
  exit 0
fi

# ── Add workflow via GitHub API ───────────────────────────────────────────────
CONTENT=$(openssl base64 -in "$WORKFLOW_SOURCE" | tr -d '\n')

for BRANCH in main master; do
  if gh api \
    --method PUT \
    "repos/$FULL_REPO/contents/$WORKFLOW_DEST" \
    --field message="$COMMIT_MSG" \
    --field content="$CONTENT" \
    --field branch="$BRANCH" \
    &>/dev/null 2>&1; then
    echo "✅ Workflow added to $FULL_REPO on branch '$BRANCH'."
    echo "   It will run on the next pull request against main/master/develop."
    exit 0
  fi
done

echo "❌ Failed to add workflow to $FULL_REPO."
echo "   Check that the repo exists, is not empty, and your token has 'contents: write' access."
exit 1
