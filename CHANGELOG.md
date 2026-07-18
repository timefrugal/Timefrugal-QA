# Changelog

All notable changes to Timefrugal-QA are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
Versions follow [Semantic Versioning](https://semver.org/).

---

## [1.2.1] — 2026-07-18

### Fixed
- **`pip-audit` scope leak** — `run_pip_audit()` no longer falls back to auditing the active Python environment (including Timefrugal-QA's own dependencies) when no manifest input is given; it now only runs when the target repo has a `requirements.txt`/`requirements-dev.txt`, passing each found manifest via `-r`. Previously any consuming repo could get phantom High-severity findings attributed to a hardcoded `requirements.txt` that didn't exist, blocking every PR touching a `.py` file
- **`setup_all_repos.sh` false failures** — when the PAT can't read a private repo (GET check fails), the script now inspects the PUT error body: a 422 response containing `"sha"` means the file already exists, so it reports skipped instead of failed

### Added
- `scripts/setup_new_repo.sh` — adds the Timefrugal-QA workflow to a single newly-created repo (companion to the bulk `setup_all_repos.sh`)
- `.github/workflows/auto-setup.yml` — daily scheduled workflow that runs `setup_all_repos.sh` to add the QA workflow to any repo missing it

### Changed
- Consumer install instructions now pin to the moving `@v1` tag instead of `@main` (`templates/repo_workflow.yml`, `.github/workflows/qa-reusable.yml`, `scripts/run_local_qa.sh`), so a commit to `main` no longer changes gating behavior fleet-wide without a manual re-tag

---

## [1.2.0] — 2026-06-13

### Added
- **Java language support** — `static_analysis.py` now runs semgrep and PMD 7+ on `.java` files; AI review uses a Java-specific prompt covering NPE, deserialization, SSRF, and generics; test generation produces JUnit 5 + Mockito output
- **HTML language support** — semgrep and htmlhint run on `.html`/`.htm` files; AI review prompt targets XSS, accessibility, CSP, and semantic structure; test generation is skipped (not applicable)
- **Automatic language detection** — `detect_language()` inspects changed file extensions and selects the appropriate toolchain without any configuration
- `PYTHON_EXTENSIONS`, `JAVA_EXTENSIONS`, `HTML_EXTENSIONS`, `SUPPORTED_EXTENSIONS` constants in `config.py`
- `PMD_CMD` and `HTMLHINT_CMD` config entries (graceful degradation if tools are absent)

### Changed
- `get_changed_python_files()` renamed to `get_changed_files()` — now picks up `.py`, `.java`, `.html`, `.htm`
- `run_all()` dispatches tools per language rather than running the full Python suite unconditionally
- `review_code()` and `generate_tests()` accept a `language` parameter; code fences in AI messages use the correct language label
- Existing test discovery (`find_existing_tests`) only runs for Python codebases

---

## [1.1.0] — 2026-06-12

### Added
- **Custom semgrep rules** — `qa_agent/semgrep_rules/` bundled in the package and loaded alongside `--config auto`; rules cover `subprocess shell=True`, `eval`/`exec`, `pickle.loads`, hardcoded secrets, missing `requests` timeout, bare `except`, and mutable default arguments
- **Parallel static analysis** — all tools now run concurrently via `ThreadPoolExecutor` in `static_analysis.py`
- **Parallel AI calls** — `review_code` and `generate_tests` run concurrently, cutting AI wait time ~50%
- **Exponential backoff retry** — `ai_review.py` retries on HTTP 429 (rate limit) with 5s/10s/20s delays
- **GitHub API retry consistency** — `pr_reporter.py` uses the same `_request_with_retry()` backoff as `ai_review.py`
- **GitHub Actions step summary** — report appended to `$GITHUB_STEP_SUMMARY` after every CI run via `pr_reporter.write_step_summary()`
- **`--commit-tests` flag** — writes AI-generated tests to `tests/` and creates a git commit (local mode only)
- **Pre-commit hook** — `.pre-commit-hooks.yaml` added for [pre-commit](https://pre-commit.com) framework integration

### Changed
- Packaging migrated from `setup.py` to `pyproject.toml` (PEP 517/518)

---

## [1.0.0] — 2026-06-11

### Added
- Initial release — AI-powered QA agent for Python repositories
- GitHub Actions reusable workflow (`qa-reusable.yml`) and caller template (`templates/repo_workflow.yml`)
- `qa_agent` Python package: `agent.py`, `static_analysis.py`, `ai_review.py`, `pr_reporter.py`, `local_reporter.py`, `config.py`
- Static analysis via bandit, semgrep, pylint, mypy, radon, pip-audit
- AI code review and pytest test generation via GitHub Models (`gpt-4o-mini`) — free with any GitHub account
- PR comment posting with deduplication (`<!-- timefrugal-qa-comment -->` marker) and commit status check
- Merge blocking on CRITICAL/HIGH findings
- Local pre-PR runner (`scripts/run_local_qa.sh`)
- Bulk repo setup scripts (`setup_all_repos.sh`, `setup_new_repo.sh`)
- Daily auto-setup workflow (`auto-setup.yml`) — adds QA workflow to any repo missing it
- Windows UTF-8 fix for PowerShell terminals

[1.2.1]: https://github.com/timefrugal/Timefrugal-QA/releases/tag/v1.2.1
[1.2.0]: https://github.com/timefrugal/Timefrugal-QA/releases/tag/v1.2.0
[1.1.0]: https://github.com/timefrugal/Timefrugal-QA/releases/tag/v1.1.0
[1.0.0]: https://github.com/timefrugal/Timefrugal-QA/releases/tag/v1.0.0
