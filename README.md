# Timefrugal-QA

AI-powered QA agent for **Python, Java, and HTML** repositories. Runs as a GitHub Actions reusable workflow **and** locally before raising a PR — catching issues early to minimize GitHub Actions usage and avoid PR iteration loops.

**Cost: $0.** Uses [GitHub Models](https://github.com/marketplace/models) (free AI with any GitHub account) and open-source analysis tools only.

---

## What it does

On every pull request (and optionally before raising one locally), the agent:

1. **Language detection** — automatically detects whether changed files are Python, Java, or HTML and selects the appropriate toolchain
2. **Static analysis** — runs the right tools per language: bandit/pylint/mypy/radon/pip-audit for Python; PMD for Java; htmlhint for HTML; semgrep runs on all three
3. **AI code review** — sends the diff + static findings to GitHub Models (`gpt-4o-mini`) with a language-specific prompt; reviews for bugs, security vulnerabilities, architecture/design issues, and performance
4. **Test generation** — generates pytest tests for Python, JUnit 5 tests for Java (HTML skips — not applicable)
4. **Reports** — posts a structured review comment on the PR, sets a commit status check (blocks merge if critical/high issues are found), and writes a formatted summary to the GitHub Actions step summary UI

---

## Architecture

```
Timefrugal/Timefrugal-QA         ← this repo (central)
├── qa_agent/                     ← Python agent package
│   └── semgrep_rules/            ← bundled custom semgrep rules
├── .github/workflows/
│   └── qa-reusable.yml           ← reusable workflow (called by other repos)
├── .pre-commit-hooks.yaml        ← pre-commit framework integration
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

Or add to a single new repo immediately:

```bash
bash scripts/setup_new_repo.sh my-new-project
# or with explicit owner:
bash scripts/setup_new_repo.sh Timefrugal/my-new-project
```

### Step 2b — Auto-setup future repos (set and forget)

Store a PAT as a repository secret so the daily scheduled workflow can add the QA agent to any new repos automatically.

1. Create a **fine-grained PAT** at GitHub → Settings → Developer settings → Personal access tokens → Fine-grained tokens:
   - Resource owner: your account
   - Repository access: **All repositories**
   - Permissions: **Contents** → Read and write
2. Add it as a secret named `GH_PAT` in this repo (Timefrugal-QA → Settings → Secrets → Actions).
3. Done. The `.github/workflows/auto-setup.yml` workflow runs daily and adds the QA workflow to any repo that's missing it. You can also trigger it manually from the Actions tab.

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

### Pre-commit hook integration

To run Timefrugal-QA automatically before every commit via the [pre-commit](https://pre-commit.com) framework, add this to your repo's `.pre-commit-config.yaml`:

```yaml
repos:
  - repo: https://github.com/timefrugal/Timefrugal-QA
    rev: main
    hooks:
      - id: timefrugal-qa
```

Then install the hook:

```bash
pip install pre-commit
pre-commit install
```

`GITHUB_TOKEN` must be set in your environment for the AI review to run.

---

## GitHub token scopes

| Context | Required scopes |
|---------|----------------|
| GitHub Actions (CI) | `GITHUB_TOKEN` is automatically provided — no setup needed |
| Local runner | Personal access token with `repo` scope (classic) or fine-grained token with read/write PR access |
| setup_all_repos.sh / setup_new_repo.sh | Personal access token with `repo` scope (classic) or fine-grained token with Contents read/write |
| auto-setup.yml (scheduled) | Fine-grained PAT stored as `GH_PAT` secret — Contents read/write across all repos |

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

## Supported languages

Language is detected automatically from the extensions of changed files.

| Language | Extensions | Static analysis tools |
|----------|-----------|----------------------|
| Python | `.py` | semgrep, bandit, pylint, mypy, radon, pip-audit |
| Java | `.java` | semgrep, PMD |
| HTML | `.html` `.htm` | semgrep, htmlhint |

PMD and htmlhint degrade gracefully if not installed — the agent logs a warning and continues with the remaining tools.

---

## Free tools used

| Tool | Language | Purpose |
|------|----------|---------|
| [GitHub Models](https://github.com/marketplace/models) | All | Free AI (`gpt-4o-mini`) — code review and test generation |
| [semgrep](https://semgrep.dev) | All | SAST — free community rules + bundled custom rules |
| [bandit](https://bandit.readthedocs.io) | Python | Security linter |
| [pylint](https://pylint.org) | Python | Code quality and bug detection |
| [mypy](https://mypy-lang.org) | Python | Static type checking |
| [radon](https://radon.readthedocs.io) | Python | Cyclomatic complexity |
| [pip-audit](https://pypi.org/project/pip-audit/) | Python | Dependency vulnerability scanning |
| [PMD](https://pmd.github.io) | Java | Static analysis — bugs, style, best practices |
| [htmlhint](https://htmlhint.com) | HTML | Linting — accessibility, structure, security |

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
