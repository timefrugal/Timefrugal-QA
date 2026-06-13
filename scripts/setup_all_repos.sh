#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# setup_all_repos.sh
#
# Adds the Timefrugal-QA workflow to every repo under your GitHub account.
# Requires: GitHub CLI (gh) — https://cli.github.com
#
# Usage:
#   export GITHUB_TOKEN=ghp_yourtoken    # or: gh auth login
#   bash scripts/setup_all_repos.sh
#
# What it does:
#   1. Lists all repos under the authenticated account
#   2. For each repo, adds .github/workflows/qa.yml via the GitHub API
#   3. Skips repos that already have the file
#   4. Skips the Timefrugal-QA repo itself
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

WORKFLOW_SOURCE="$(dirname "$0")/../templates/repo_workflow.yml"
WORKFLOW_DEST=".github/workflows/qa.yml"
COMMIT_MSG="chore: add Timefrugal-QA automated review workflow"
BRANCH="add-timefrugal-qa"
SKIP_REPO="Timefrugal-QA"

# ── Preflight ────────────────────────────────────────────────────────────────
if ! command -v gh &>/dev/null; then
  echo "❌ GitHub CLI (gh) is not installed. Install from https://cli.github.com"
  exit 1
fi

if ! gh auth status &>/dev/null; then
  echo "❌ Not authenticated. Run: gh auth login"
  exit 1
fi

GH_USER=$(gh api user --jq '.login')
echo "✅ Authenticated as: $GH_USER"
echo ""

# ── Get all repos ────────────────────────────────────────────────────────────
echo "📋 Fetching repositories for $GH_USER..."
REPOS=$(gh repo list "$GH_USER" --limit 200 --json name --jq '.[].name')
TOTAL=$(echo "$REPOS" | wc -l | tr -d ' ')
echo "Found $TOTAL repositories."
echo ""

ADDED=0
SKIPPED=0
FAILED=0

for REPO in $REPOS; do
  FULL_REPO="$GH_USER/$REPO"

  # Skip the QA agent repo itself
  if [ "$REPO" = "$SKIP_REPO" ]; then
    echo "⏭️  Skipping $REPO (this is the QA repo)"
    ((SKIPPED++)) || true
    continue
  fi

  # Check if workflow already exists
  if gh api "repos/$FULL_REPO/contents/$WORKFLOW_DEST" &>/dev/null 2>&1; then
    echo "⏭️  $REPO — workflow already exists, skipping"
    ((SKIPPED++)) || true
    continue
  fi

  # Read and base64-encode the workflow file (portable: macOS/Linux)
  CONTENT=$(openssl base64 -in "$WORKFLOW_SOURCE" | tr -d '\n')

  _try_add() {
    local branch="$1"
    local err
    err=$(gh api \
      --method PUT \
      "repos/$FULL_REPO/contents/$WORKFLOW_DEST" \
      --field message="$COMMIT_MSG" \
      --field content="$CONTENT" \
      --field branch="$branch" \
      2>&1)
    local rc=$?
    if [ $rc -eq 0 ]; then
      echo "✅ $REPO — workflow added (branch: $branch)"
      ((ADDED++)) || true
      return 0
    fi
    # 422 "sha wasn't supplied" means the file already exists
    if echo "$err" | grep -q "sha"; then
      echo "⏭️  $REPO — workflow already exists (branch: $branch), skipping"
      ((SKIPPED++)) || true
      return 0
    fi
    return 1
  }

  if _try_add main || _try_add master; then
    : # handled inside _try_add
  else
    echo "❌ $REPO — failed to add workflow (check permissions or empty repo)"
    ((FAILED++)) || true
  fi
done

echo ""
echo "─────────────────────────────────────"
echo "Summary:"
echo "  ✅ Added:   $ADDED"
echo "  ⏭️  Skipped: $SKIPPED"
echo "  ❌ Failed:  $FAILED"
echo "─────────────────────────────────────"
echo ""
echo "Done! Each repo will now run Timefrugal-QA on every pull request."
