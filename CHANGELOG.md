# Changelog

All notable changes to Timefrugal-QA are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
Versions follow [Semantic Versioning](https://semver.org/).

---

## [Unreleased]

### Fixed (this entry)
- **`--model` CLI flag was a complete silent no-op** — `python -m qa_agent --model <id>` set `os.environ["QA_AI_MODEL"]` and nothing else, but `qa_agent.config` reads every `QA_AI_MODEL*` env var at module-*import* time into module-level constants, and config is already fully imported before `argparse` even runs (transitively, via `__main__.py`'s top-level `from qa_agent.agent import run`). The assignment landed after the values it was meant to influence were already bound, so the flag was ignored — no error, no warning, the review just silently ran on whatever model resolved at import. Broken for as long as the flag has existed (confirmed via `git blame`; found while reviewing the unrelated Groq-default-tag fix, filed separately as issue #19 rather than folded in). `README.md`, `CLAUDE.md`, `scripts/run_local_qa.sh` and the flag's own `--help` text all advertised behavior the code never delivered.

  Fix: `__main__.py` now also mutates `config.AI_PROVIDERS[i]["model"]` in place for every entry (plus `config.AI_MODEL`, the constant that seeds the Groq entry) right after argparse. No lazy-evaluation redesign of `config.py`'s import-time contract was needed: every module reads config as `from qa_agent import config` and dereferences `config.AI_PROVIDERS` as a module attribute at call time — `ai_review._configured_providers`/`_call_with_fallback` included — so the in-place mutation propagates as-is, and `ai_review.py` is untouched. Applied to **all four** providers, not just Groq, because the documented contract is "override the model for whichever provider ends up handling the request", and the request can land on any tier of the fallback chain. Setting `model` on an unconfigured slot stays inert: `_configured_providers` requires `api_key` AND `base_url` AND `model` all non-empty. The now-redundant `os.environ` write is kept deliberately — it's the documented knob, and it's what a child process or a later `importlib.reload(config)` would read.

  One thing deliberately NOT changed: `config.QA_FALLBACK_MODEL`. `ai_review` compares `model == config.QA_FALLBACK_MODEL` as an identity marker to scope the fallback-only `think:false` `extra_body` and JSON-schema `response_format` to that slot; rewriting it to the override value would make *every* provider match and send fallback-only params to Groq/Cerebras/Mistral, which their strict schema validation can reject. Accepted consequence: under `--model`, the fallback tier runs without that special-casing — the tuning belongs to the configured fallback model, not to one the caller substituted.

  Regression coverage: new `tests/test_main.py` (first test of `__main__`'s argument handling — this CLI path was entirely uncovered) drives the real `main()` with `run()` mocked at its `qa_agent.__main__` binding, asserting the override reaches all four provider entries and `config.AI_MODEL`, that the env var is still set, that an unconfigured fallback slot is still skipped by `_configured_providers`, that `QA_FALLBACK_MODEL` is left alone, and that omitting `--model` changes nothing. Mutation check: reverting the two new mutation lines in `__main__.py` makes `test_model_flag_reaches_every_provider_in_the_chain` fail.

### Fixed (this entry)
- **Groq (tier 1 of the 4-provider chain) 404'd on every single QA run** — `config.AI_MODEL`'s default was `llama-3.3-70b-versatile`, which Groq has since retired along with its entire llama chat lineup. Because `ai_review._call_with_fallback` treats any provider failure as "move to the next one," this failed *silently*: no consumer repo ever saw an error, every real review just quietly started at Cerebras or Mistral instead, and the Groq key/quota bought nothing. Found via jarvis-infra issue #309. Default is now `openai/gpt-oss-120b` — Groq's own recommended general-purpose/reasoning replacement, and the same model family already running one hop down the chain (`AI_MODEL_CEREBRAS`) and on jarvis-infra's primary Ollama box (Z13/jarvis-1 Slot A), so quality expectations are a known quantity rather than a fresh unknown. Note the `openai/` namespace prefix: Groq requires it, Cerebras does not (plain `gpt-oss-120b`) — the two config values differ deliberately.

  Tradeoff worth knowing: Groq's free tier gives `openai/gpt-oss-120b` 8K TPM / 200K TPD, *lower* than the retired model's 12K TPM, so tier 1 now has less headroom before a 429/413 sends the chain to Cerebras (`openai/gpt-oss-20b` has the identical 8K TPM, so downsizing would buy back *no* headroom — only capability lost). The `config.py` comments and `README.md`/`CLAUDE.md` tables carrying the old tag and the old 12K figure were updated to match; the two `--model llama-3.1-8b-instant` doc examples (also a retired Groq llama tag, i.e. copy-pasteably broken) now use `openai/gpt-oss-20b`. Historical entries in this changelog that mention the old tag are left as-is on purpose — they're a record of what actually shipped at the time.

  Regression coverage: `tests/test_config.py`'s `TestGroqDefaultModel` reloads the real `config` module with `QA_AI_MODEL` unset to assert the new default reaches `AI_PROVIDERS[0]["model"]` under the `groq` name, and with it set to assert the override still wins end-to-end.

### Fixed (this entry)
- **AI reviewer had no diff boundary, causing repeated fabricated/escalated CRITICAL/HIGH findings against pre-existing code** — `review_code()` previously sent only full (truncated) file content plus the static-analysis summary; the model had no way to distinguish a line the PR actually introduced from code that had been sitting there for months, and no instruction against restating a real static MEDIUM/LOW finding at an invented higher severity. Confirmed on three separate real PRs (jarvis-infra #306, #307, #310): 0 Critical/0 High in the real static-analysis table each time, yet the free-form AI section fabricated 1-6 Critical/High findings per PR, every single one mapping to code entirely outside that PR's actual diff when checked line-by-line — including, on one PR, an "AI-Generated Test Cases" section referencing classes (`GPUHeadroomSampler`, `ModelSlotManager`) that don't exist anywhere in the reviewed file.

  Two-part fix, since a prompt instruction alone isn't reliable enough for a decision that blocks a merge on a small/free-tier model: (1) `agent.py`'s new `get_diff_text()`/`get_changed_line_ranges()` compute the real `git diff` for the PR (text form for the prompt, per-file changed-line-number sets for a structural check), threaded through `review_code()`'s new optional `diff_text`/`changed_line_ranges` params (both default to today's behavior when omitted — no breaking change for existing callers). (2) `review_code()`'s new `_demote_if_outside_diff()` runs AFTER the AI responds, independent of how well any given provider followed the prompt: any CRITICAL/HIGH finding whose `file:line` isn't actually inside this PR's diff (or has an unverifiable `line: 0`) gets demoted to MEDIUM — kept visible as a suggestion, stripped of its blocking power. Same principle `AIReview.has_blocking_issues` already applies at the whole-review grain ("AI findings shouldn't independently block with unvalidated severity"), applied one level deeper, per finding.

  Regression coverage: `tests/test_agent.py`'s `TestGetDiffText`/`TestGetChangedLineRanges` verify the new git-diff helpers against real temp-repo fixtures (single-line adds, mid-file modifies, pure deletions, multi-hunk files, unrelated-file exclusion). `tests/test_ai_review.py`'s `TestDemoteIfOutsideDiff` covers the demotion logic in isolation (inside/outside diff, unknown file, line-0, MEDIUM-and-below never touched, `None` ranges = no-op for backward compat); `TestReviewCodeDiffScopingEndToEnd` and `TestDiffSectionInReviewPrompt` drive the real `review_code()` pipeline end-to-end (fake OpenAI client only) to confirm the demotion and prompt-section wiring actually work together, not just in isolation. Mutation check performed manually while writing this fix: reverting the `_demote_if_outside_diff()` call site makes `TestReviewCodeDiffScopingEndToEnd::test_ai_finding_outside_diff_is_demoted_in_the_real_pipeline` fail.

### Added (this entry)
- **Fourth, last-resort AI provider slot (`QA_FALLBACK_BASE_URL` / `QA_FALLBACK_API_KEY` / `QA_FALLBACK_MODEL`)** — `config.AI_PROVIDERS` gains a 4th entry, tried only after Groq, Cerebras, AND Mistral have all failed/are all unconfigured. Unlike the three named cloud providers, this slot is generic (no default base URL, no default model) — the first real consumer is jarvis-infra routing to Z13 (a Tailscale-reachable home GPU box running an Ollama-compatible OpenAI-SDK-shaped gateway) as a true last resort, added directly in response to a real incident (jarvis-infra issue #200: Groq's org-wide daily quota exhaustion cascaded into a Cerebras JSON-parse crash, forcing an admin-bypass on an otherwise-clean PR). Silently absent unless all three `QA_FALLBACK_*` vars are set — zero behavior change for any repo that doesn't configure it. Unlike the other three providers (gated purely on `api_key`, since their `base_url`/`model` always come from a real default), `_configured_providers` explicitly requires `api_key` AND `base_url` AND `model` all non-empty for this slot, so a partial config (e.g. only `QA_FALLBACK_API_KEY` set) is cleanly treated as unconfigured rather than failing confusingly deep inside the openai SDK (caught in independent review before merge).

### Fixed (this entry)
- **JSON parsing happened AFTER the provider fallback loop, not inside it** — a provider that responds HTTP 200 with unparseable/garbage content counted as a transport-level "success," so the chain never advanced to the next provider even though the response was useless. This is exactly what happened during the jarvis-infra issue #200 incident: Cerebras returned garbage after Groq's quota was exhausted, and the whole QA gate crashed instead of falling through to Mistral. JSON parsing (`_parse_review_json`, extracted from the inline markdown-fence-stripping + `json.loads` that used to run after `_call_with_fallback` returned) now happens inside each provider's own attempt (`review_code`'s `_make_request` closure), so a garbage-but-200 response is treated as that provider's failure and the loop advances, same as a network error or non-200 status would. **This changes real behavior for every repo using the existing Groq → Cerebras → Mistral chain, not just jarvis-infra or repos that configure the new 4th slot above** — it's on by default the moment this merges to `main`, since every consumer installs the tool from `main`/`@v1` on each QA run.
- **Two deeper failure modes found in independent review of the fix above, before merge**: (1) valid-but-non-object JSON (a bare list/string/number all parse successfully via `json.loads`) used to be returned as-is, so `review_code()`'s later `data.get(...)` calls would raise `AttributeError` OUTSIDE the try/except meant to catch provider failures — crashing the whole review instead of falling through, the exact bug class this fix exists to close, one layer deeper. (2) empty/whitespace-only content used to default to `"{}"` (a valid, clean, zero-findings review) instead of being treated as a failure — a silent false pass, worse than a crash, and the fourth provider's own known failure mode (a locally-hosted model burning its whole token budget on hidden reasoning with empty visible output). `_parse_review_json` now rejects both explicitly, and `review_code`'s `_make_request` no longer pre-substitutes `"{}"` for empty content before calling it.

Regression coverage: `tests/test_ai_review.py`'s `TestGarbageJsonFromEarlierProviderFallsThroughToNextProvider` drives the real `review_code` → `_call_with_fallback` → `_parse_review_json` pipeline against a faked OpenAI client (not a re-implementation of the fix) for all three failure shapes (garbage, non-object, empty), and the original garbage-JSON fallthrough tests were confirmed to fail against the pre-fix code before being merged. `TestParseReviewJsonRejectsGarbageEmptyAndNonObjectContent` and `TestConfiguredProvidersRequiresBaseUrlAndModelTooForGenericSlots` cover the two review-round fixes at the unit level.
- **mypy: `raise last_error` where `last_error: Optional[Exception]`** — `_call_with_fallback`'s final raise, added by the provider-chain work below, was flagged by mypy since the type technically allows `None` even though the preceding loop always assigns it. Added an `assert last_error is not None` immediately before the raise, which mypy's narrowing understands.
- **`filter_ignored` couldn't precisely waive radon findings** — found while trying to waive two pre-existing HIGH complexity findings (`agent.py:run()` complexity 21, `pr_reporter.py:_build_comment()` complexity 26 — both predate this session's changes, just pulled into diff scope by unrelated edits to the same files) via `.timefrugal-qa.yml`. radon's `rule_id` is a fixed `"CC"` for every complexity finding regardless of function/file, so the existing rule_id-based waiver would have silently disabled complexity checking for the *entire* repo, not just these two functions. `filter_ignored` now also matches a `"file:line"` entry in the same ignore list — unambiguous against real rule_ids/CVEs/GHSAs, none of which contain `:`. The two findings above are now waived precisely by location in `.timefrugal-qa.yml`, with the actual refactor tracked separately in issue #10 (deferred rather than rushed — no local test-execution environment was available this session to safely validate a refactor of unfamiliar 21/26-branch functions).
- **Stale "Sending to Groq AI" log message** — hardcoded a specific provider name/model that's no longer accurate now that the AI call goes through a fallback chain; the actual provider/model used is only known once `_call_with_fallback` runs. Now says "AI provider chain" generically.

### Removed
- The unused `workflow_call`-triggered workflow under `.github/workflows/` — dead code: confirmed no `uses:` reference anywhere in this repo (every consumer installs the self-contained `templates/repo_workflow.yml` copy instead), and it was missing `models: read` permission, so AI calls would have 403'd had it ever been invoked. Reviving a true cross-repo reusable-workflow-call pattern is a deliberate future architecture decision, not done here.

### Changed
- **AI review backend switched from GitHub Models to a fallback chain of free providers (Groq → Cerebras → Mistral)** — GitHub fully retired GitHub Models (playground, catalog, inference API, BYOK) on 2026-07-30 for every customer, no exceptions (brownouts ran 2026-07-16/23 as warning). Every PR reaching the AI-review step (any PR touching real code, not just docs/YAML) had started failing the whole check with a 401/410 depending on which of the two Models endpoints got hit — first surfaced via a consumer repo (jarvis-infra, PR #63), which was the first real-code PR to run since the deprecation began, so it had gone undetected until then.

  `qa_agent` now tries each configured provider in `config.AI_PROVIDERS` order — [Groq](https://console.groq.com) (`llama-3.3-70b-versatile`, 12K TPM), then [Cerebras](https://cloud.cerebras.ai) (`gpt-oss-120b`, 30K TPM), then [Mistral](https://console.mistral.ai) (`mistral-small-latest`) — all OpenAI-compatible, so only base URL/model/key differ. A single-provider design turned out not to be enough headroom on its own: this PR's own diff hit Groq's 12K TPM ceiling outright mid-development (see the 413 fix below), which is exactly the scenario the fallback chain exists for. `ai_review._call_with_fallback` moves to the next provider on any failure (after that provider's own `_call_with_retry` 429-retries are exhausted); a provider with no API key configured is silently skipped, not an error — only zero configured providers fails closed with a clear message.

  **Action for every consumer repo**: add at least one of `GROQ_API_KEY` / `CEREBRAS_API_KEY` / `MISTRAL_API_KEY` as a repo secret — more than one buys real headroom, but one is enough to keep working. `templates/repo_workflow.yml` and this repo's own `qa.yml` pass all three through (secrets a repo doesn't have just resolve empty, no error); `models: read` permission removed (no longer needed). All docs (`README.md`, `CLAUDE.md`, `scripts/run_local_qa.sh`) updated to match. Note: Cerebras' free tier requires adding a payment method on their side to get free credits; Mistral's free "Experiment" tier requires opting into data training on your inputs — both are the account owner's call, not this tool's.
- **Doc/reality mismatch** — `CLAUDE.md`'s "Architecture decisions" section claimed target repos have "a tiny 15-line caller workflow" and that "all agent improvements auto-apply to every repo." Corrected to describe what actually ships: `qa_agent`'s Python logic auto-updates for consumers via the `@v1` pip install pin, but the workflow YAML (`templates/repo_workflow.yml`) is a one-time install-time copy that `auto-setup.yml`'s skip-if-exists check does not refresh later. `README.md` updated to match.
- **Local/CI parity visibility** — `local_reporter.print_report()` now prints an explicit warning when one or more analysis tools failed to run locally, pointing at the Tool Warnings block above it, so a local PASS with tool failures isn't mistaken for a guaranteed CI PASS (CI treats the same tool failures as a distinct `errored` state that fails the check).

### Fixed
- **`.timefrugal-qa.yml` pip-audit waivers silently never matched CVE-based entries** — found while validating the Groq switch above: this repo's own waiver list (3 `mcp` CVEs, investigated and accepted as unreachable) was still showing up as blocking on every run. Root cause: pip-audit's OSV-based `id` field is frequently a GHSA-* identifier, with the CVE number present only in a separate `aliases` array that `Finding` never captured — so a waiver written against the CVE (the natural thing to write, since that's what advisory descriptions lead with) never matched `rule_id` and silently never took effect. `Finding` now carries `aliases`, `run_pip_audit` populates it from pip-audit's own output, and `filter_ignored` matches against `rule_id` OR any alias. Affects every consumer repo using CVE-based pip-audit waivers, not just this repo's own.
- **AI review 413'd on PRs touching several files** — also found while validating the Groq switch (this PR's own 9-file diff triggered it): `review_code`/`generate_tests` truncated each changed file to a fixed 6000/5000 chars regardless of how many files changed, so total prompt size scaled unboundedly with file count. This PR's diff requested ~15,500 tokens against Groq's free-tier `llama-3.3-70b-versatile` limit of 12,000 TPM and got a hard 413 — and `review_code`/`generate_tests` run *concurrently* (`agent.py`'s `ThreadPoolExecutor`), sharing the same per-org TPM budget within the same minute, compounding it further. New `_per_file_char_budget()` splits a total character budget (`QA_AI_MAX_TOTAL_CONTENT_CHARS`, default 16000) across however many files changed — small file counts keep the original fixed caps unchanged, larger ones scale down per-file so the total stays roughly bounded. Affects every consumer repo on any PR sized like this one, not just this repo's own.

### Added
- 5 regression tests: pip-audit command scoping to the target project, AI review severity validation/fallback, commit-status `blocked`-over-`errored` precedence, agent exit-code precedence, and `run_all`'s error-isolation-without-dropping-findings behavior.
- 4 more regression tests for this round: `run_pip_audit` capturing `aliases` from tool output (present and absent cases), and `filter_ignored` matching a waiver via alias (both the positive match and confirming it doesn't over-match unrelated CVEs).
- 4 more regression tests: `_per_file_char_budget` keeping small-file-count behavior unchanged, splitting the budget and staying bounded for many files, respecting the floor, and not raising on zero files.
- 7 more regression tests for the provider chain: `_configured_providers` filtering by API-key presence, `_call_with_fallback` leaving a successful first provider alone, falling back on failure, skipping an unconfigured middle provider without calling it, raising the last provider's own error when all fail, and failing closed with a clear message when zero providers are configured.

---

## [1.2.1] — 2026-07-18

### Fixed
- **`pip-audit` scope leak** — `run_pip_audit()` no longer falls back to auditing the active Python environment (including Timefrugal-QA's own dependencies) when no manifest input is given; it now only runs when the target repo has a `requirements.txt`/`requirements-dev.txt`, passing each found manifest via `-r`. Previously any consuming repo could get phantom High-severity findings attributed to a hardcoded `requirements.txt` that didn't exist, blocking every PR touching a `.py` file
- **`setup_all_repos.sh` false failures** — when the PAT can't read a private repo (GET check fails), the script now inspects the PUT error body: a 422 response containing `"sha"` means the file already exists, so it reports skipped instead of failed

### Added
- `scripts/setup_new_repo.sh` — adds the Timefrugal-QA workflow to a single newly-created repo (companion to the bulk `setup_all_repos.sh`)
- `.github/workflows/auto-setup.yml` — daily scheduled workflow that runs `setup_all_repos.sh` to add the QA workflow to any repo missing it

### Changed
- Consumer install instructions now pin to the moving `@v1` tag instead of `@main` (`templates/repo_workflow.yml`, `scripts/run_local_qa.sh`; the unused reusable-workflow file also carried this pin before its removal — see Unreleased), so a commit to `main` no longer changes gating behavior fleet-wide without a manual re-tag

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
- GitHub Actions reusable workflow (`workflow_call` trigger; never actually invoked by any consumer and removed 2026-07-18 as dead code — see Unreleased) and caller template (`templates/repo_workflow.yml`)
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
