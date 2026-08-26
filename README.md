# Timefrugal-QA

AI-powered QA agent for **Python, Java, HTML, JavaScript, and TypeScript** repositories. Runs as a GitHub Actions reusable workflow **and** locally before raising a PR — catching issues early to minimize GitHub Actions usage and avoid PR iteration loops.

**Cost: $0.** Uses a chain of free-tier AI providers (Groq → Cerebras → Mistral — see below) and open-source analysis tools only. (Previously used GitHub Models, which GitHub fully retired 2026-07-30.)

---

## What it does

On every pull request (and optionally before raising one locally), the agent:

1. **Language detection** — automatically detects the dominant language among changed files (Python, Java, HTML, JavaScript, or TypeScript) and selects the appropriate toolchain
2. **Static analysis** — runs the right tools per language: bandit/pylint/mypy/radon/pip-audit for Python; PMD for Java; htmlhint for HTML; ESLint for JavaScript/TypeScript plus `tsc --noEmit` type-checking for TypeScript; semgrep runs on all of them
3. **AI code review** — sends the diff + static findings to the first configured AI provider (Groq, then Cerebras, then Mistral, falling back automatically if one is out of quota) with a language-specific prompt; reviews for bugs, security vulnerabilities, architecture/design issues, and performance
4. **Test generation** — generates pytest tests for Python, JUnit 5 tests for Java, Jest tests for JavaScript/TypeScript (HTML skips — not applicable)
4. **Reports** — posts a structured review comment on the PR, sets a commit status check (blocks merge if critical/high issues are found), and writes a formatted summary to the GitHub Actions step summary UI

---

## Architecture

```
Timefrugal/Timefrugal-QA         ← this repo (central)
├── qa_agent/                     ← Python agent package
│   └── semgrep_rules/            ← bundled custom semgrep rules
├── .github/workflows/
│   └── auto-setup.yml            ← daily: adds the QA workflow to any repo missing it
├── .pre-commit-hooks.yaml        ← pre-commit framework integration
├── templates/
│   └── repo_workflow.yml         ← self-contained workflow copied into each repo
└── scripts/
    ├── run_local_qa.sh           ← local pre-PR runner
    └── setup_all_repos.sh        ← adds workflow to all your repos at once
```

Each target repo gets its own `.github/workflows/qa.yml`, installed from `templates/repo_workflow.yml` at setup time. The Python QA logic (`qa_agent`) lives in this one repo and consumers pull it via `pip install git+...@v1`, so agent-code improvements reach every repo on their next run. The workflow YAML itself is a one-time copy, though — it is **not** auto-refreshed later; picking up workflow-level changes requires re-running `setup_all_repos.sh`/`setup_new_repo.sh` against already-installed repos.

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
export GROQ_API_KEY=gsk_yourkey   # free key: https://console.groq.com/keys
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
| `--model <id>` | Override the AI model for whichever provider ends up handling the request (e.g. `--model openai/gpt-oss-20b`) |
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

At least one of `GROQ_API_KEY` / `CEREBRAS_API_KEY` / `MISTRAL_API_KEY` must be set in your environment for the AI review to run.

---

## AI provider chain

AI review tries providers in order — Groq first, then Cerebras, then Mistral, then an optional 4th last-resort provider — falling back automatically to the next one if a provider is out of quota, down, returns unparseable content, or simply not configured. You only need **one** key to get AI review working; adding more just buys headroom against any single free tier's rate limits (this is exactly what happened during development: a single PR's diff hit Groq's 12K TPM ceiling outright).

| Provider | Free tier | Get a key |
|----------|-----------|-----------|
| [Groq](https://console.groq.com) | `openai/gpt-oss-120b`, 8K TPM, 200K TPD | [console.groq.com/keys](https://console.groq.com/keys) |
| [Cerebras](https://cloud.cerebras.ai) | `gpt-oss-120b`, 30K TPM, 1M TPD | [cloud.cerebras.ai](https://cloud.cerebras.ai) — free credits require adding a payment method on Cerebras' side, worth knowing before signing up |
| [Mistral](https://console.mistral.ai) | `mistral-small-latest` ("Experiment" tier) | [console.mistral.ai](https://console.mistral.ai) — the free Experiment tier requires opting into data training on your inputs to unlock its full quota |
| 4th: last-resort fallback (`QA_FALLBACK_*`) | Whatever you point it at | Not a named cloud service — a generic OpenAI-SDK-compatible endpoint you control (base URL, key, and model all self-supplied). Only reached once Groq, Cerebras, AND Mistral have all failed or are unconfigured. Added for consumer repos that want a self-hosted/private last resort (e.g. jarvis-infra's Z13 gateway) rather than relying purely on free cloud tiers. |

A provider whose key isn't set is silently skipped, not an error — only having zero configured providers fails closed with a clear message. A provider that responds but returns content that doesn't parse as the expected review JSON is also treated as a failure for that provider (not a silent pass-through), so the chain still advances to the next one.

## Token/key requirements

| Context | Required |
|---------|----------------|
| GitHub Actions (CI) | `GITHUB_TOKEN` is automatically provided for PR comments/commit status — no setup needed. At least one AI provider key must be added as a repo secret for the AI review step (see Step 2b-equivalent: add it under Settings → Secrets → Actions) |
| Local runner | At least one AI provider key (see table above) |
| setup_all_repos.sh / setup_new_repo.sh | Personal access token with `repo` scope (classic) or fine-grained token with Contents read/write |
| auto-setup.yml (scheduled) | Fine-grained PAT stored as `GH_PAT` secret — Contents read/write across all repos |

---

## Configuration

All config is via environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `QA_AI_MODEL` | `openai/gpt-oss-120b` | Groq model (first provider in the chain) |
| `QA_AI_MODEL_CEREBRAS` | `gpt-oss-120b` | Cerebras model (fallback) |
| `QA_AI_MODEL_MISTRAL` | `mistral-small-latest` | Mistral model (fallback) |
| `QA_FALLBACK_BASE_URL` | _(none)_ | Base URL for the 4th, last-resort fallback provider — **required together with** `QA_FALLBACK_API_KEY`/`QA_FALLBACK_MODEL`; the provider is only considered configured once all three are set (a partial set is treated as unconfigured, not an error) |
| `QA_FALLBACK_API_KEY` | _(none)_ | Auth for the 4th, last-resort fallback provider |
| `QA_FALLBACK_MODEL` | _(none)_ | Model name for the 4th, last-resort fallback provider |
| `QA_AI_MAX_TOKENS` | `3000` | Max tokens per AI response |
| `QA_AI_RETRY_MAX_ATTEMPTS` | `3` | Retries on rate-limit (HTTP 429), per provider, before falling back to the next one |
| `QA_AI_RETRY_BASE_DELAY` | `5.0` | Base delay in seconds between retries (doubles each attempt) |
| `QA_MAX_COMPLEXITY` | `10` | Cyclomatic complexity threshold |
| `QA_REPORT_FILE` | `qa_report.md` | Local report output path |

To use a different (still free) model for any provider, set the matching `QA_AI_MODEL`/`QA_AI_MODEL_CEREBRAS`/`QA_AI_MODEL_MISTRAL` env var — see each provider's own docs for current model catalogs.

---

## Supported languages

Language is detected automatically from the extensions of changed files.

| Language | Extensions | Static analysis tools |
|----------|-----------|----------------------|
| Python | `.py` | semgrep, bandit, pylint, mypy, radon, pip-audit |
| Java | `.java` | semgrep, PMD |
| HTML | `.html` `.htm` | semgrep, htmlhint |
| JavaScript | `.js` `.jsx` `.mjs` `.cjs` | semgrep, ESLint |
| TypeScript | `.ts` `.tsx` | semgrep, ESLint, tsc (type-checking) |

PMD, htmlhint, ESLint, and tsc all degrade gracefully if not installed — the agent logs a warning and continues with the remaining tools. Note: ESLint's default parser cannot parse TypeScript syntax without the target repo configuring `@typescript-eslint/parser` itself (standard ESLint/TypeScript setup) — without it, `.ts`/`.tsx` files get a parse-error finding from ESLint instead of real lint results, though `tsc` type-checking is unaffected either way.

---

## Free tools used

| Tool | Language | Purpose |
|------|----------|---------|
| [Groq](https://console.groq.com) / [Cerebras](https://cloud.cerebras.ai) / [Mistral](https://console.mistral.ai) | All | Free AI provider chain, automatic fallback — code review and test generation |
| [semgrep](https://semgrep.dev) | All | SAST — free community rules + bundled custom rules |
| [bandit](https://bandit.readthedocs.io) | Python | Security linter |
| [pylint](https://pylint.org) | Python | Code quality and bug detection |
| [mypy](https://mypy-lang.org) | Python | Static type checking |
| [radon](https://radon.readthedocs.io) | Python | Cyclomatic complexity |
| [pip-audit](https://pypi.org/project/pip-audit/) | Python | Dependency vulnerability scanning |
| [PMD](https://pmd.github.io) | Java | Static analysis — bugs, style, best practices |
| [htmlhint](https://htmlhint.com) | HTML | Linting — accessibility, structure, security |
| [ESLint](https://eslint.org) | JavaScript/TypeScript | Linting — quality + `eslint-plugin-security` rules |
| [tsc](https://www.typescriptlang.org) | TypeScript | Type-checking (`--noEmit`) |

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
GitHub Actions runs the installed qa.yml workflow (final gate)
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

To change the blocking threshold, set it per-repo — **don't** edit `config.py` in this central repo, that would change it for every consumer. Either add to your repo's own `.timefrugal-qa.yml`:
```yaml
block_merge_threshold: MEDIUM   # stricter
```
or set the `QA_BLOCK_MERGE_THRESHOLD` env var (same values: `CRITICAL`/`HIGH`/`MEDIUM`/`LOW`). The per-repo config takes precedence over the env var, which takes precedence over the built-in default (`HIGH`).
