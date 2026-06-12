# Timefrugal-QA — Claude Code Handoff

## Project purpose
AI-powered QA agent for Python repos. Runs as a GitHub Actions reusable workflow AND locally before raising a PR. Zero cost — uses GitHub Models free AI (`gpt-4o-mini`) and open-source static analysis tools only.

**Owner:** github.com/Timefrugal  
**Target language:** Python  
**Status:** Live and end-to-end tested (published 2026-06-11)

---

## What was built (all files exist in this directory)

```
Timefrugal-QA/
├── qa_agent/
│   ├── __init__.py          # version = 1.0.0
│   ├── __main__.py          # CLI: python -m qa_agent [--ci|--base|--no-tests|--commit-tests|--model]
│   ├── config.py            # all config via env vars (GITHUB_TOKEN, QA_AI_MODEL, etc.)
│   ├── agent.py             # orchestrator: git diff → static analysis → AI review → report
│   ├── static_analysis.py  # runs bandit, semgrep, pylint, mypy, radon, pip-audit
│   ├── ai_review.py         # GitHub Models API (OpenAI-compatible, GITHUB_TOKEN auth)
│   ├── pr_reporter.py       # posts PR comment + sets commit status check via GitHub API
│   └── local_reporter.py   # rich terminal output + saves qa_report.md
├── .github/workflows/
│   └── qa-reusable.yml      # reusable workflow (workflow_call trigger)
├── templates/
│   └── repo_workflow.yml    # copy this to .github/workflows/qa.yml in each target repo
├── scripts/
│   ├── run_local_qa.sh      # local pre-PR runner (uses GITHUB_TOKEN or gh CLI)
│   └── setup_all_repos.sh  # bulk-adds workflow to all repos via gh CLI + GitHub API
├── pyproject.toml           # pip-installable: pip install git+https://github.com/Timefrugal/Timefrugal-QA.git@main
├── requirements.txt         # openai, bandit, semgrep, pylint, mypy, radon, pip-audit, rich, requests
└── README.md
```

---

## Architecture decisions (don't change without reason)

- **Free AI:** GitHub Models at `https://models.inference.ai.azure.com`, authenticated with `GITHUB_TOKEN` (no extra billing). Default model: `gpt-4o-mini`. Configurable via `QA_AI_MODEL` env var.
- **Reusable workflow pattern:** Logic lives in ONE repo (`Timefrugal-QA`). Target repos have a tiny 15-line caller workflow. All agent improvements auto-apply to every repo.
- **Blocking threshold:** CRITICAL + HIGH severity → blocks merge. MEDIUM/LOW → advisory. Controlled by `BLOCK_MERGE_THRESHOLD` in `config.py`.
- **PR comment deduplication:** `pr_reporter.py` looks for an existing comment with the marker `<!-- timefrugal-qa-comment -->` and updates it rather than appending a new one on each push.
- **Token budget:** AI responses capped at 3000 tokens (`QA_AI_MAX_TOKENS`) and file content truncated at 6000 chars per file to stay within free rate limits.
- **Local-first workflow:** The intended usage pattern is run `run_local_qa.sh` → fix issues → raise PR. This minimises GitHub Actions minutes consumed.

---

## Immediate next steps (priority order)

### 1. Publish to GitHub (REQUIRED before anything else works)
```bash
cd /path/to/Timefrugal-QA
git init
git add .
git commit -m "feat: initial Timefrugal-QA agent"
gh repo create Timefrugal/Timefrugal-QA --public --source=. --push
```

### 2. Smoke-test locally
```bash
export GITHUB_TOKEN=$(gh auth token)
pip install -e .
pip install bandit semgrep pylint mypy radon pip-audit
# Run against a test repo that has some Python changes staged
cd /path/to/some-python-repo
python -m qa_agent --base main
```

### 3. Apply to all repos
```bash
export GITHUB_TOKEN=$(gh auth token)
bash scripts/setup_all_repos.sh
```

### 4. Test CI workflow end-to-end
- Raise a test PR in any Python repo that now has `qa.yml`
- Verify the PR comment appears and commit status is set

---

## Known gaps / future improvements

- **Go/JS support:** Currently Python-only. `static_analysis.py` can be extended with language detection + `gosec`/`eslint` runners.
- **GitHub org-level required workflows:** If the account is upgraded to an org, replace `setup_all_repos.sh` with a GitHub org-level required workflow setting (Settings → Actions → Required workflows). Cheaper to maintain.
- **Semgrep rules:** Currently uses `--config auto` (community rules). Can point to a custom ruleset in this repo at `semgrep-rules/` for organisation-specific patterns.

### Completed improvements
- ~~pyproject.toml support~~ — migrated from `setup.py` (2026-06-12)
- ~~Parallel tool execution~~ — `static_analysis.py` now uses `ThreadPoolExecutor` (2026-06-12)
- ~~Rate limit handling~~ — exponential backoff on HTTP 429 in `ai_review.py` (2026-06-11)
- ~~Test file auto-commit~~ — `--commit-tests` flag added (2026-06-12)

---

## Key environment variables

| Variable | Where set | Purpose |
|----------|-----------|---------|
| `GITHUB_TOKEN` | Auto in Actions; manual locally | Auth for GitHub Models AI + GitHub API |
| `QA_AI_MODEL` | Optional | Override model (default: `gpt-4o-mini`) |
| `QA_AI_MAX_TOKENS` | Optional | Max AI response tokens (default: 3000) |
| `QA_AI_RETRY_MAX_ATTEMPTS` | Optional | Retries on rate-limit HTTP 429 (default: 3) |
| `QA_AI_RETRY_BASE_DELAY` | Optional | Base retry delay in seconds, doubles each attempt (default: 5.0) |
| `QA_MAX_COMPLEXITY` | Optional | Cyclomatic complexity threshold (default: 10) |
| `QA_REPORT_FILE` | Optional | Local report output path (default: `qa_report.md`) |
| `GITHUB_REPOSITORY` | Auto in Actions | `owner/repo` string |
| `GITHUB_SHA` | Auto in Actions | Commit SHA for status checks |
| `PR_NUMBER` | Auto in Actions | Set from `github.event.pull_request.number` |

---

## How to run locally (quick reference)

```bash
# From inside any Python project directory:
export GITHUB_TOKEN=ghp_yourtoken

# Full review (default: diff vs origin/main)
python -m qa_agent

# Diff vs a different branch, skip test generation
python -m qa_agent --base develop --no-tests

# Write generated tests to tests/ and commit them
python -m qa_agent --commit-tests

# Use a more powerful model
python -m qa_agent --model gpt-4o
```

### All CLI flags

| Flag | Description |
|------|-------------|
| `--base <ref>` | Diff against a different branch or commit (default: `origin/main`) |
| `--no-tests` | Skip AI test case generation |
| `--commit-tests` | Write generated tests to `tests/` and commit (local mode only) |
| `--model <id>` | Override the GitHub Models AI model |
| `--ci` | CI mode — posts PR comment + sets commit status |
| `--pr <number>` | PR number (CI mode; set automatically in Actions) |
| `--root <path>` | Project root directory (default: `.`) |

---

## How to run in CI (GitHub Actions)

The reusable workflow is called automatically when a PR is opened against `main`/`master`/`develop`. No manual steps needed once `setup_all_repos.sh` has run.

To trigger manually for debugging:
```bash
gh workflow run qa.yml --repo Timefrugal/some-repo
```
