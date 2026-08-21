# Timefrugal-QA — Claude Code Handoff

## Project purpose
AI-powered QA agent for Python, Java, and HTML repos. Runs as a GitHub Actions reusable workflow AND locally before raising a PR. Zero cost — uses a chain of free-tier AI providers (Groq → Cerebras → Mistral, automatic fallback, plus an optional 4th `QA_FALLBACK_*`-configured last-resort provider — see "AI provider chain" below) and open-source static analysis tools only. (Was GitHub Models until GitHub fully retired it 2026-07-30; see CHANGELOG.md [Unreleased].)

**Owner:** github.com/Timefrugal  
**Target languages:** Python, Java, HTML (auto-detected from changed file extensions)  
**Status:** Live and end-to-end tested (published 2026-06-11)

---

## What was built (all files exist in this directory)

```
Timefrugal-QA/
├── qa_agent/
│   ├── __init__.py          # version = 1.2.0
│   ├── __main__.py          # CLI: python -m qa_agent [--ci|--base|--no-tests|--commit-tests|--model]
│   ├── config.py            # all config via env vars; AI_PROVIDERS chain (GROQ_API_KEY, CEREBRAS_API_KEY, MISTRAL_API_KEY, QA_AI_MODEL*, etc.)
│   ├── agent.py             # orchestrator: git diff → static analysis → AI review → report
│   ├── static_analysis.py  # language detection + per-language tool runners (parallel); Python: bandit/pylint/mypy/radon/pip-audit; Java: PMD; HTML: htmlhint; all: semgrep
│   ├── semgrep_rules/
│   │   ├── python-security.yml  # subprocess shell=True, eval/exec, pickle, hardcoded secrets, requests timeout
│   │   └── python-quality.yml   # bare except, mutable default args
│   ├── ai_review.py         # provider-chain fallback (Groq→Cerebras→Mistral, all OpenAI-compatible); language-aware review + test prompts (Python→pytest, Java→JUnit 5, HTML skips tests)
│   ├── pr_reporter.py       # posts PR comment + sets commit status check via GitHub API
│   └── local_reporter.py   # rich terminal output + saves qa_report.md
├── .github/workflows/
│   └── auto-setup.yml       # scheduled daily: adds QA workflow to any repo missing it (needs GH_PAT secret)
├── templates/
│   └── repo_workflow.yml    # copy this to .github/workflows/qa.yml in each target repo
├── scripts/
│   ├── run_local_qa.sh      # local pre-PR runner (needs at least one of GROQ_API_KEY/CEREBRAS_API_KEY/MISTRAL_API_KEY)
│   ├── setup_all_repos.sh  # bulk-adds workflow to all repos via gh CLI + GitHub API
│   └── setup_new_repo.sh   # adds workflow to a single named repo (usage: bash setup_new_repo.sh owner/repo)
├── .pre-commit-hooks.yaml   # pre-commit framework integration
├── pyproject.toml           # pip-installable: pip install git+https://github.com/Timefrugal/Timefrugal-QA.git@v1
├── requirements.txt         # openai, bandit, semgrep, pylint, mypy, radon, pip-audit, rich, requests
└── README.md
```

---

## Architecture decisions (don't change without reason)

- **Free AI, provider chain with automatic fallback:** `config.AI_PROVIDERS` tries Groq (`api.groq.com/openai/v1`, `GROQ_API_KEY`) → Cerebras (`api.cerebras.ai/v1`, `CEREBRAS_API_KEY`) → Mistral (`api.mistral.ai/v1`, `MISTRAL_API_KEY`) → an optional 4th last-resort provider (`QA_FALLBACK_BASE_URL`/`QA_FALLBACK_API_KEY`/`QA_FALLBACK_MODEL`) in order, all OpenAI-compatible. `ai_review._call_with_fallback` moves to the next provider on any failure from the current one (after that provider's own `_call_with_retry` 429 retries are exhausted); a provider with no API key set is skipped, not an error. Added 2026-07-30 after a single PR's diff hit Groq's 12K TPM ceiling outright — a single free-tier backend wasn't enough headroom on its own. (Switched from GitHub Models 2026-07-30 when GitHub fully retired it — see CHANGELOG.md.) The `QA_FALLBACK_*` 4th slot was added after a real incident (jarvis-infra issue #200): Groq's org-wide daily quota exhausted, cascading into a Cerebras JSON-parse crash that took down the whole gate. Unlike the three named cloud providers, it has no default base URL/model — it's a generic OpenAI-SDK-compatible slot, silently absent unless all three env vars are set. **JSON parsing now happens INSIDE each provider's own attempt** (`review_code`'s `_make_request`/`ai_review._parse_review_json`), not after `_call_with_fallback` returns — a provider that responds HTTP 200 with unparseable content is treated as that provider's failure so the chain still advances (this is what actually happened with Cerebras during the issue #200 incident, and is a behavior change affecting every repo on this chain, not just ones that configure the 4th slot).
- **Per-repo installable pattern:** Python logic lives in ONE repo (`qa_agent`), installed via `pip install git+...@v1` — auto-updates for consumers whenever they re-run their workflow (since the pin resolves at install time). The workflow YAML itself (`templates/repo_workflow.yml`) is copied into each target repo at install time and is NOT automatically refreshed later — `auto-setup.yml`'s skip-if-exists check means workflow-level changes require manually re-running `setup_all_repos.sh`/`setup_new_repo.sh` against already-installed repos.
- **Blocking threshold:** CRITICAL + HIGH severity → blocks merge. MEDIUM/LOW → advisory. Controlled by `BLOCK_MERGE_THRESHOLD` in `config.py`.
- **PR comment deduplication:** `pr_reporter.py` looks for an existing comment with the marker `<!-- timefrugal-qa-comment -->` and updates it rather than appending a new one on each push.
- **Token budget:** AI responses capped at 3000 tokens (`QA_AI_MAX_TOKENS`) and file content truncated at 6000 chars per file to stay within free rate limits.
- **Local-first workflow:** The intended usage pattern is run `run_local_qa.sh` → fix issues → raise PR. This minimises GitHub Actions minutes consumed.
- **Language detection:** `detect_language()` in `static_analysis.py` counts file extensions in the changed set and returns the dominant language (`python` / `java` / `html`). Mixed-language diffs are handled by running all applicable tool sets.
- **Per-language toolchain:** `run_all()` splits files by extension before dispatching — semgrep runs on all three; Python-only tools (bandit, pylint, mypy, radon, pip-audit) are skipped for Java/HTML; PMD runs only for `.java`; htmlhint runs only for `.html`/`.htm`. All tools degrade gracefully if not installed.
- **Language-aware AI prompts:** `ai_review.py` holds separate system prompts per language. Java prompt emphasises NPE, deserialization, SSRF, generics; HTML prompt focuses on XSS, accessibility, CSP, semantic structure.
- **Test generation scope:** Python → pytest; Java → JUnit 5 + Mockito; HTML → skipped (no applicable test framework).
- **Custom semgrep rules:** `qa_agent/semgrep_rules/` is bundled in the package and loaded automatically alongside `--config auto`. The bundled rules target Python (`language: python`) and are silently ignored by semgrep for Java/HTML files. Add new `.yml` files to extend coverage without changing any Python code.
- **Parallel AI calls:** `review_code` and `generate_tests` run concurrently via `ThreadPoolExecutor` in `agent.py` — cuts AI wait time ~50% when test generation is enabled.
- **GitHub Actions step summary:** `pr_reporter.write_step_summary()` appends the report to `$GITHUB_STEP_SUMMARY` after every CI run. No-op locally (env var not set).
- **API retry consistency:** All GitHub API calls in `pr_reporter.py` use `_request_with_retry()` — same 5s/10s/20s exponential backoff on HTTP 429 as `ai_review.py`.

---

## Known gaps / future improvements

- **Go/JS support:** Currently Python/Java/HTML. `static_analysis.py` can be extended with additional language entries + `gosec`/`eslint` runners following the same pattern.
- **GitHub org-level required workflows:** If the account is upgraded to an org, replace `setup_all_repos.sh` with a GitHub org-level required workflow setting (Settings → Actions → Required workflows). Cheaper to maintain.
- **New repo detection:** Currently the daily `auto-setup.yml` polls all repos rather than reacting to a `repository.created` webhook — real-time triggering would require a GitHub App or org-level webhook.

### Architecture review findings (Fable, 2026-07-17)

Method: full read of `qa_agent/*.py`, both workflows, `scripts/`, `templates/`, semgrep rules; scope-consistency sweep of every tool runner against the pip-audit ambient-env bug class; blocking-path trace (`has_blocking_issues` → `set_commit_status`). Docs-only change — no code fixed here. file:line refs are as of this commit.

- **[CRITICAL] Gate fails open on tool failure** — every runner turns missing-tool / JSON-parse / semgrep-network failures into non-blocking `errors` (`static_analysis.py:94-95,148-150,190-192,229-231,340-341`); radon swallows parse errors with no record at all (`static_analysis.py:301-304`). A broken tool = silent PASS fleet-wide. Fix: in CI mode, tool-execution errors must fail the status check (or set a distinct error state); keep graceful degradation local-only.
- **[CRITICAL] Unpinned fleet rollout** — targets install `git+...@main` (`templates/repo_workflow.yml:53`, `qa-reusable.yml:79`, `run_local_qa.sh`) and `auto-setup.yml` pushes workflows daily to every repo's default branch with a broad `GH_PAT`. One bad/compromised commit on main instantly changes (or hijacks — the installed code sees each repo's `GITHUB_TOKEN`) gating everywhere; no canary, no rollback beyond reverting main. Fix: semver tags, template pins `@vX.Y.Z` (or a moving `@v1` major tag), auto-setup advances the pin only after a canary repo runs `@main` green; add `qa.yml` to this repo itself — `setup_all_repos.sh:57` skips it, making the gatekeeper the only ungated repo.
- **[CRITICAL — verified; fix in flight on separate branch] pip-audit audits the runner venv, not the target repo** — no `-r`, cwd only (`static_analysis.py:330-331`), findings hardcoded to `requirements.txt:0` (`static_analysis.py:350-351`); CI installs this repo's own deps into the same venv first (`templates/repo_workflow.yml:50-56`), so target PRs were blocked on Timefrugal-QA's CVEs (live-confirmed on jarvis-infra PR #28). Also triggers on any `.py` change, not on dependency-file changes (`static_analysis.py:469`).
- **[HIGH] AI findings independently block merge** — `agent.py:182` ORs in `ai_review.has_blocking_issues` (`ai_review.py:38-42`); severity strings from free-tier `gpt-4o-mini` are taken verbatim, unvalidated (`ai_review.py:255`). Nondeterministic, prompt-injectable (the reviewed code IS the prompt), and its failure mode is a silent pass. Fix: AI layer advisory-only by default (`QA_AI_BLOCKING` env flag), validate the severity enum, never let AI alone flip the check.
- **[HIGH] Severity mappings systematically over-block** — mypy `error`→HIGH (`static_analysis.py:267`), pylint `E`→HIGH (`:121`), semgrep `WARNING`→HIGH (`:106`), radon CC>20→HIGH (`:312`), pip-audit "fix exists"→HIGH (`:345`, treats fixability as severity — no CVSS/exploitability). Concrete: a bare `except:` (bundled `python-quality.yml`, WARNING) blocks merge; any repo not mypy-clean, or whose deps aren't in the runner venv (pylint `E0401`, mypy missing-stubs), is permanently blocked — those two are the same ambient-environment leak class as pip-audit. Fix: WARNING→MEDIUM, complexity advisory-only, pip-audit severity from OSV/CVSS data; make `BLOCK_MERGE_THRESHOLD` env-configurable (hardcoded constant, `config.py:43`, despite the env-var table implying tunability).
- **[HIGH] No waiver path outside bandit `# nosec`** — semgrep/pylint/mypy have native inline ignores, but pip-audit's `--ignore-vuln` is never plumbed through, radon CC has no inline ignore, and AI findings cannot be waived at all; no per-repo config exists. An accepted-risk finding can only be silenced by disabling the whole check. Fix: `.timefrugal-qa.yml` in target repos → ignored vuln IDs, complexity threshold, per-tool severity overrides, AI-advisory flag.
- **[MEDIUM] Whole-file scope + two-dot diff** — tools and the AI see full files, so pre-existing issues in a touched file block unrelated PRs; `get_changed_files` uses two-dot `git diff base HEAD` (`agent.py:25`), which on stale local branches also pulls in files changed only on main. Fix: three-dot/merge-base diff; longer-term filter findings to changed line ranges. Note `--root` is only honored by pip-audit — git diff and file reads use cwd (`agent.py:24-25`).
- **[MEDIUM] Doc contradicts deployment** — "Reusable workflow pattern... tiny 15-line caller" above is not what ships: `templates/repo_workflow.yml` is an 85-line self-contained copy (its line 5: "intentionally self-contained"), so `qa-reusable.yml` is dead code — and it lacks `models: read`, so AI calls would 403 there anyway. Per-repo copies + auto-setup's skip-if-exists check (`setup_all_repos.sh:64`) mean old template versions are frozen forever — improvements do NOT "auto-apply to every repo" as claimed. (fixed 2026-07-18, see "Per-repo installable pattern" in Architecture decisions above; the dead `qa-reusable.yml` file referenced here has since been deleted)
- **[MEDIUM] Local/CI parity + version drift** — locally, missing tools degrade to warnings, so local PASS ≠ CI verdict; `pyproject.toml` says 1.1.0 while `__init__.py` says 1.2.0 — cosmetic while installs track `@main`, load-bearing once pinning lands.
- **[MEDIUM] Zero self-tests** — `find . -iname "*test*.py"` returns nothing. Highest-leverage first test: `test_run_pip_audit_is_scoped_to_target_project` — monkeypatch `_run` to capture the command, call `run_pip_audit(tmp_project)`, assert the command references the target's own dependency manifest (`-r <tmp>/requirements.txt`) and that no finding names a package absent from that manifest (e.g. `rich`/`openai` from the tool's own venv). This exact test catches the shipped bug class pre-ship.

### Completed improvements
- ~~pyproject.toml support~~ — migrated from `setup.py` (2026-06-12)
- ~~Parallel tool execution~~ — `static_analysis.py` now uses `ThreadPoolExecutor` (2026-06-12)
- ~~Rate limit handling~~ — exponential backoff on HTTP 429 in `ai_review.py` (2026-06-11)
- ~~Test file auto-commit~~ — `--commit-tests` flag added (2026-06-12)
- ~~Semgrep custom rules~~ — `qa_agent/semgrep_rules/` bundled in package, run alongside `--config auto` (2026-06-12)
- ~~GitHub Actions step summary~~ — report written to `$GITHUB_STEP_SUMMARY` in CI (2026-06-12)
- ~~Parallel AI calls~~ — `review_code` + `generate_tests` run concurrently (2026-06-12)
- ~~GitHub API retry~~ — `pr_reporter.py` retries on HTTP 429, consistent with `ai_review.py` (2026-06-12)
- ~~Pre-commit hook~~ — `.pre-commit-hooks.yaml` added for pre-commit framework integration (2026-06-12)
- ~~Version bump~~ — 1.0.0 → 1.1.0 (2026-06-12)
- ~~Version bump~~ — 1.1.0 → 1.2.0 (2026-06-13)
- ~~Java and HTML language support~~ — language detection, PMD (Java), htmlhint (HTML), language-aware AI prompts and test generation (2026-06-13)

---

## Key environment variables

| Variable | Where set | Purpose |
|----------|-----------|---------|
| `GITHUB_TOKEN` | Auto in Actions; manual locally | Auth for GitHub API (PR comments, commit status) |
| `GROQ_API_KEY` | At least one of these three required (CI: repo secret; local: manual) | Auth for Groq (1st in provider chain) |
| `CEREBRAS_API_KEY` | Optional | Auth for Cerebras (2nd, fallback) |
| `MISTRAL_API_KEY` | Optional | Auth for Mistral (3rd, fallback) |
| `QA_FALLBACK_BASE_URL` | Optional | Base URL for the 4th, last-resort fallback provider (e.g. jarvis-infra's Z13 gateway) -- must be set together with `QA_FALLBACK_API_KEY` and `QA_FALLBACK_MODEL` for this slot to activate |
| `QA_FALLBACK_API_KEY` | Optional | Auth for the 4th, last-resort fallback provider -- only reached once Groq, Cerebras, AND Mistral have all failed/are unconfigured |
| `QA_FALLBACK_MODEL` | Optional | Model name for the 4th, last-resort fallback provider -- no default (unlike the three named cloud providers, this slot isn't tied to a fixed service) |
| `QA_AI_MODEL` | Optional | Override Groq model (default: `openai/gpt-oss-120b`) |
| `QA_AI_MODEL_CEREBRAS` | Optional | Override Cerebras model (default: `gpt-oss-120b`) |
| `QA_AI_MODEL_MISTRAL` | Optional | Override Mistral model (default: `mistral-small-latest`) |
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
# From inside any Python, Java, or HTML project directory:
export GROQ_API_KEY=gsk_yourkey

# Full review (default: diff vs origin/main)
python -m qa_agent

# Diff vs a different branch, skip test generation
python -m qa_agent --base develop --no-tests

# Write generated tests to tests/ and commit them
python -m qa_agent --commit-tests

# Use a different model on whichever provider handles the request
python -m qa_agent --model openai/gpt-oss-20b
```

### All CLI flags

| Flag | Description |
|------|-------------|
| `--base <ref>` | Diff against a different branch or commit (default: `origin/main`) |
| `--no-tests` | Skip AI test case generation |
| `--commit-tests` | Write generated tests to `tests/` and commit (local mode only) |
| `--model <id>` | Override the AI model for whichever provider handles the request |
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
