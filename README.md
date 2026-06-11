# Timefrugal-QA

AI-powered QA agent for Python repositories. Runs as a GitHub Actions reusable workflow **and** locally before raising a PR — catching issues early to minimize GitHub Actions usage and avoid PR iteration loops.

**Cost: $0.** Uses [GitHub Models](https://github.com/marketplace/models) (free AI with any GitHub account) and open-source analysis tools only.

---

## What it does

On every pull request (and optionally before raising one locally), the agent:

1. **Static analysis** — runs bandit, semgrep, pylint, mypy, radon, and pip-audit on changed Python files
2. **AI code review** — sends the diff + static findings to GitHub Models (`gpt-4o-mini`) acting as a senior engineer with 15+ years of experience; reviews for bugs, security vulnerabilities, architecture/design issues, and performance
3. **Test generation** — generates comprehensive pytest test cases for changed code
4. **Reports** — posts a structured review comment on the PR and sets a commit status check (blocks merge if critical/high issues are found)

---

## Architecture

```
Timefrugal/Timefrugal-QA         ← this repo (central)
├── qa_agent/                     ← Python agent package
├── .github/workflows/
│   └── qa-reusable.yml           ← reusable workflow (called by other repos)
├── templates/
│   └── repo_workflow.yml         ← template to copy into each repo
└── scripts/
    ├── run_local_qa.sh           ← local pre-PR runner
    └── setup_all_repos.sh        ← adds workflow to all your repos at once
```

Each target repo has a tiny `.github/workflows/qa.yml` that calls the central reusable workflow. All QA logic lives in one place — improvements automatically apply to every repo.

---

## Quick start

### Step 1 — Publish this repo to GitHub

Push this directory to `github.com/Timefrugal/Timefrugal-QA`.

```bash
cd /path/to/Timefrugal-QA
git init
git add .
git commit -m "feat: initial Timefrugal-QA agent"
git remote add origin https://github.com/Timefrugal/Timefrugal-QA.git
git push -u origin main
```

### Step 2 — Add the workflow to all your repos (one command)

```bash
export GITHUB_TOKEN=ghp_yourtoken   # needs repo scope
bash scripts/setup_all_repos.sh
```

Or add manually to a single repo by copying `templates/repo_workflow.yml` to `.github/workflows/qa.yml`.

### Step 3 — Run locally before every PR

From inside any of your project directories:

```bash
export GITHUB_TOKEN=ghp_yourtoken   # or: gh auth login
bash /path/to/Timefrugal-QA/scripts/run_local_qa.sh
```

If it exits with code 0, raise the PR. If not, fix the issues first.

---

## GitHub token scopes

| Context | Required scopes |
|---------|----------------|
| GitHub Actions (CI) | `GITHUB_TOKEN` is automatically provided — no setup needed |
| Local runner | Personal access token with `repo` scope (classic) or fine-grained token with read/write PR access |
| setup_all_repos.sh | Personal access token with `repo` scope |

---

## Configuration

All config is via environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `QA_AI_MODEL` | `gpt-4o-mini` | GitHub Models AI model |
| `QA_AI_MAX_TOKENS` | `3000` | Max tokens per AI response |
| `QA_MAX_COMPLEXITY` | `10` | Cyclomatic complexity threshold |
| `QA_REPORT_FILE` | `qa_report.md` | Local report output path |

To use a more powerful (but still free) model:

```yaml
# in templates/repo_workflow.yml
with:
  ai-model: "gpt-4o"   # higher quality, lower rate limit
```

---

## Free tools used

| Tool | Purpose |
|------|---------|
| [GitHub Models](https://github.com/marketplace/models) | Free AI (`gpt-4o-mini`) — code review and test generation |
| [bandit](https://bandit.readthedocs.io) | Python security linter |
| [semgrep](https://semgrep.dev) | SAST — free community rules |
| [pylint](https://pylint.org) | Code quality and bug detection |
| [mypy](https://mypy-lang.org) | Static type checking |
| [radon](https://radon.readthedocs.io) | Cyclomatic complexity |
| [pip-audit](https://pypi.org/project/pip-audit/) | Dependency vulnerability scanning |

---

## Workflow for iterating on code (cost-saving approach)

```
Write code
    ↓
Run locally:  bash scripts/run_local_qa.sh
    ↓
Fix issues flagged by agent
    ↓
Re-run until QA passes (exit 0)
    ↓
Raise PR on GitHub
    ↓
GitHub Actions runs qa-reusable.yml (final gate)
    ↓
Merge ✅
```

This avoids multiple PR commits triggered by CI failures — each of which consumes GitHub Actions minutes.

---

## Severity levels and merge blocking

| Level | Example | Blocks merge? |
|-------|---------|--------------|
| CRITICAL | SQL injection, hardcoded secret | ✅ Yes |
| HIGH | Security vulnerability, serious bug | ✅ Yes |
| MEDIUM | Code smell, missing error handling | ❌ No |
| LOW | Style issue, minor refactor suggestion | ❌ No |
| INFO | Architecture note | ❌ No |

To change the blocking threshold, edit `config.py`:
```python
BLOCK_MERGE_THRESHOLD = SEVERITY_MEDIUM   # stricter
```
