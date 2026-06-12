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

## CLI reference

```bash
python -m qa_agent [options]
```

| Flag | Description |
|------|-------------|
| _(none)_ | Diff vs `origin/main`, full review, tests shown in terminal |
| `--base <ref>` | Diff against a different branch or commit (e.g. `--base develop`) |
| `--no-tests` | Skip AI test case generation |
| `--commit-tests` | Write generated tests to `tests/` and commit them (local mode only) |
| `--model <id>` | Override the GitHub Models AI model (e.g. `--model gpt-4o`) |
| `--ci` | CI mode — posts PR comment and sets commit status instead of terminal output |
| `--pr <number>` | PR number, used with `--ci` (set automatically in GitHub Actions) |
| `--root <path>` | Project root directory (default: current directory) |

### `--commit-tests` behaviour

When passed, the agent writes the AI-generated pytest file to `tests/` and creates a git commit:

- Single file changed → `tests/test_<filename>.py`
- Multiple files changed → `tests/test_changes.py`

If the commit fails (e.g. a pre-commit hook rejects it) the error is printed and the rest of the QA report continues normally.

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
| `QA_AI_RETRY_MAX_ATTEMPTS` | `3` | Retries on GitHub Models rate-limit (HTTP 429) |
| `QA_AI_RETRY_BASE_DELAY` | `5.0` | Base delay in seconds between retries (doubles each attempt) |
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
| [semgrep](https://semgrep.dev) | SAST — free community rules + bundled custom rules |
| [pylint](https://pylint.org) | Code quality and bug detection |
| [mypy](https://mypy-lang.org) | Static type checking |
| [radon](https://radon.readthedocs.io) | Cyclomatic complexity |
| [pip-audit](https://pypi.org/project/pip-audit/) | Dependency vulnerability scanning |

---

## Custom semgrep rules

Bundled rules in `qa_agent/semgrep_rules/` run automatically alongside the free community ruleset. No configuration needed.

### python-security.yml

| Rule | Severity | What it catches |
|------|----------|-----------------|
| `subprocess-shell-true` | HIGH | `subprocess` called with `shell=True` |
| `eval-use` | CRITICAL | Any use of `eval()` |
| `exec-use` | CRITICAL | Any use of `exec()` |
| `pickle-deserialize` | CRITICAL | `pickle.loads()` / `pickle.load()` |
| `requests-no-timeout` | HIGH | `requests` calls missing a `timeout` argument |
| `hardcoded-secret` | CRITICAL | String assigned to a variable named `password`, `api_key`, `token`, etc. |

### python-quality.yml

| Rule | Severity | What it catches |
|------|----------|-----------------|
| `bare-except` | HIGH | `except:` with no exception type specified |
| `mutable-default-arg` | HIGH | `def f(x=[], ...)` or `def f(x={}, ...)` |

### Adding your own rules

Drop any `.yml` file into `qa_agent/semgrep_rules/` following the [semgrep rule syntax](https://semgrep.dev/docs/writing-rules/rule-syntax). It will be picked up automatically on the next run — no code changes needed.

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
