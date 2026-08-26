"""
Tests for qa_agent.static_analysis.

Uses stdlib unittest (no pytest / test framework is set up in this repo yet),
following the convention established in tests/test_repo_config.py and
tests/test_agent.py.
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from qa_agent import config, static_analysis
from qa_agent.static_analysis import (
    AnalysisResults,
    Finding,
    blocking_severity_label,
    detect_language,
    effective_block_threshold,
    run_all,
    run_eslint,
    run_mypy,
    run_pip_audit,
    run_pylint,
    run_semgrep,
    run_tsc,
)


class TestRunPipAuditIsScopedToTargetProject(unittest.TestCase):
    """
    M2/round-3 finding: pip-audit must audit the TARGET project's own declared
    dependencies (via `-r <manifest>`, run with cwd=<target>), never fall back
    to auditing whatever environment the QA agent's own process happens to be
    running in. Regression coverage for the shipped bug where pip-audit was
    invoked with no `-r` and no cwd, silently auditing the runner's venv.
    """

    def test_run_pip_audit_is_scoped_to_target_project(self):
        captured = {}

        def fake_run(cmd, cwd=None):
            captured["cmd"] = cmd
            captured["cwd"] = cwd
            # Empty pip-audit JSON output shape -- no findings, no error.
            return 0, '{"dependencies": []}', ""

        with tempfile.TemporaryDirectory() as tmp_dir:
            (Path(tmp_dir) / "requirements.txt").write_text("requests==2.0.0\n")

            with mock.patch.object(static_analysis, "_run", side_effect=fake_run):
                results = run_pip_audit(tmp_dir)

        self.assertIsInstance(results, AnalysisResults)
        self.assertEqual(results.errors, [])

        # cwd passed to _run must be the target project directory.
        self.assertEqual(captured["cwd"], tmp_dir)

        # The command must reference the manifest via -r <relative manifest>,
        # not an absolute path into some other environment, and must not
        # silently omit -r entirely (the shipped bug class).
        cmd = captured["cmd"]
        self.assertIn("-r", cmd)
        r_index = cmd.index("-r")
        self.assertEqual(cmd[r_index + 1], "requirements.txt")


class TestRunPipAuditCapturesAliases(unittest.TestCase):
    """
    Regression coverage: pip-audit's OSV-based `id` is frequently a GHSA-*
    identifier, with the CVE number only present in `aliases`. A repo's
    .timefrugal-qa.yml waiver list is naturally written against CVE IDs
    (that's what vulnerability descriptions lead with) -- if aliases aren't
    captured onto the Finding, such a waiver silently never matches and the
    finding stays blocking forever, even though it was investigated and
    accepted. See repo_config.filter_ignored for the matching side of this.
    """

    def test_aliases_captured_from_pip_audit_output(self):
        fake_output = (
            '{"dependencies": [{"name": "mcp", "version": "1.23.3", "vulns": '
            '[{"id": "GHSA-x5r2-r74c-3w28", '
            '"aliases": ["CVE-2026-52870"], '
            '"description": "task isolation bypass", '
            '"fix_versions": ["1.27.2"]}]}]}'
        )

        def fake_run(cmd, cwd=None):
            return 0, fake_output, ""

        with tempfile.TemporaryDirectory() as tmp_dir:
            (Path(tmp_dir) / "requirements.txt").write_text("mcp==1.23.3\n")
            with mock.patch.object(static_analysis, "_run", side_effect=fake_run):
                results = run_pip_audit(tmp_dir)

        self.assertEqual(len(results.findings), 1)
        finding = results.findings[0]
        self.assertEqual(finding.rule_id, "GHSA-x5r2-r74c-3w28")
        self.assertEqual(finding.aliases, ["CVE-2026-52870"])

    def test_missing_aliases_key_defaults_to_empty_list(self):
        fake_output = (
            '{"dependencies": [{"name": "requests", "version": "2.0.0", "vulns": '
            '[{"id": "PYSEC-2026-0001", '
            '"description": "some issue", '
            '"fix_versions": ["2.1.0"]}]}]}'
        )

        def fake_run(cmd, cwd=None):
            return 0, fake_output, ""

        with tempfile.TemporaryDirectory() as tmp_dir:
            (Path(tmp_dir) / "requirements.txt").write_text("requests==2.0.0\n")
            with mock.patch.object(static_analysis, "_run", side_effect=fake_run):
                results = run_pip_audit(tmp_dir)

        self.assertEqual(results.findings[0].aliases, [])


class TestRunSemgrepSurfacesConfigErrors(unittest.TestCase):
    """
    Regression coverage for the bug that let python-quality.yml's invalid
    YAML silently zero out semgrep findings for ~2.5 months: semgrep exits
    non-zero (e.g. rc=7) on an invalid rule config, but still prints valid
    JSON to stdout with an empty "results" list and a populated "errors"
    array describing the config problem. The old code only checked
    `rc == -1` before trusting stdout, so that JSON parsed fine, "results"
    was (correctly, but misleadingly) empty, and the error describing *why*
    was silently discarded. run_semgrep must append every reported error
    onto results.errors instead of dropping it.
    """

    def test_config_error_is_surfaced_even_with_zero_results(self):
        # Real shape of `semgrep scan --json` output when one config file in
        # a --config <dir> is invalid: nonzero rc, empty "results", populated
        # "errors" -- captured directly from a reproduction against a broken
        # rules dir.
        fake_output = (
            '{"results": [], "errors": ['
            '{"code": 5, "level": "error", "type": "SemgrepError", '
            '"message": "Invalid YAML file rules/bad.yml:\\n\\tmapping values '
            'are not allowed here\\n\\t  in \\"<file>\\", line 4, column 46"}, '
            '{"code": 7, "level": "error", "type": "SemgrepError", '
            '"message": "invalid configuration file found (1 configs were invalid)"}'
            ']}'
        )

        def fake_run(cmd, cwd=None):
            return 7, fake_output, ""

        with mock.patch.object(static_analysis, "_run", side_effect=fake_run):
            results = run_semgrep(["app.py"], project_root=".")

        # No findings (semgrep genuinely produced none) -- but that must NOT
        # read as "clean". Both config errors must be surfaced.
        self.assertEqual(results.findings, [])
        self.assertEqual(len(results.errors), 2)
        self.assertTrue(any("Invalid YAML file" in e for e in results.errors))
        self.assertTrue(any("invalid configuration file found" in e for e in results.errors))
        # Follows the existing "semgrep: <message>" convention used on the
        # rc == -1 path, for consistency in how errors render downstream.
        for e in results.errors:
            self.assertTrue(e.startswith("semgrep: "))

    def test_clean_run_with_no_errors_key_still_works(self):
        # A normal, fully-successful run's JSON has no "errors" key at all
        # (or an empty one) -- must not raise or spuriously append anything.
        fake_output = '{"results": []}'

        def fake_run(cmd, cwd=None):
            return 0, fake_output, ""

        with mock.patch.object(static_analysis, "_run", side_effect=fake_run):
            results = run_semgrep(["app.py"], project_root=".")

        self.assertEqual(results.findings, [])
        self.assertEqual(results.errors, [])


class TestRunAllAggregatesErrorsWithoutDroppingFindings(unittest.TestCase):
    """
    Regression coverage: if one tool runner inside run_all's ThreadPoolExecutor
    raises an unexpected exception, that must be recorded in combined.errors
    without silently dropping the findings any *other* tool successfully
    produced in the same run.
    """

    def test_run_all_aggregates_errors_without_dropping_findings(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            py_file = Path(tmp_dir) / "app.py"
            py_file.write_text("print('hi')\n")

            bandit_finding = Finding(
                tool="bandit",
                severity="HIGH",
                category="security",
                file="app.py",
                line=1,
                message="fake bandit finding",
            )
            pylint_finding = Finding(
                tool="pylint",
                severity="MEDIUM",
                category="quality",
                file="app.py",
                line=2,
                message="fake pylint finding",
            )

            def fake_bandit(files, project_root="."):
                raise RuntimeError("bandit exploded")

            def fake_pylint(files, repo_config=None, project_root="."):
                return AnalysisResults(findings=[pylint_finding])

            def fake_mypy(files, repo_config=None, project_root="."):
                return AnalysisResults()

            def fake_radon(files, project_root="."):
                return AnalysisResults()

            def fake_pip_audit(project_root="."):
                return AnalysisResults()

            def fake_semgrep(files, project_root="."):
                return AnalysisResults(findings=[bandit_finding])

            with mock.patch.object(static_analysis, "run_bandit", side_effect=fake_bandit), \
                 mock.patch.object(static_analysis, "run_pylint", side_effect=fake_pylint), \
                 mock.patch.object(static_analysis, "run_mypy", side_effect=fake_mypy), \
                 mock.patch.object(static_analysis, "run_radon", side_effect=fake_radon), \
                 mock.patch.object(static_analysis, "run_pip_audit", side_effect=fake_pip_audit), \
                 mock.patch.object(static_analysis, "run_semgrep", side_effect=fake_semgrep):
                combined = run_all(["app.py"], project_root=tmp_dir)

        # The exception from the broken runner (bandit) must be recorded...
        self.assertTrue(any("bandit" in e for e in combined.errors))
        # ...but findings from the OTHER successful runners (pylint, semgrep)
        # must still be present, not silently dropped.
        self.assertIn(pylint_finding, combined.findings)
        self.assertIn(bandit_finding, combined.findings)


class TestEffectiveBlockThresholdAndLabel(unittest.TestCase):
    """
    jarvis-infra#323: the PR-comment header hardcoded "Critical/High issues
    require attention" no matter what cutoff actually fired, so a repo that
    set `block_merge_threshold: MEDIUM` got a BLOCKED header naming severities
    that appeared zero times in its own summary table. These cover the shared
    helpers both reporters now derive that wording from.
    """

    def setUp(self):
        # BLOCK_MERGE_THRESHOLD is read from QA_BLOCK_MERGE_THRESHOLD at import
        # time; pin it so these assertions don't depend on the caller's env.
        patcher = mock.patch.object(config, "BLOCK_MERGE_THRESHOLD", config.SEVERITY_HIGH)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _finding(self, severity, category="quality"):
        return Finding(
            tool="pylint", severity=severity, category=category,
            file="a.py", line=1, message="x", rule_id="W0000",
        )

    def test_default_threshold_high_finding_keeps_critical_high_wording(self):
        results = AnalysisResults(findings=[self._finding("HIGH")])
        threshold = effective_block_threshold(results, ai_blocking=False)
        self.assertEqual(threshold, "HIGH")
        self.assertEqual(blocking_severity_label(threshold), "Critical/High")

    def test_repo_configured_medium_threshold_says_medium_or_above(self):
        results = AnalysisResults(
            findings=[self._finding("MEDIUM")], block_merge_threshold="MEDIUM"
        )
        threshold = effective_block_threshold(results, ai_blocking=False)
        self.assertEqual(threshold, "MEDIUM")
        label = blocking_severity_label(threshold)
        self.assertEqual(label, "Medium-or-above")
        self.assertNotIn("Critical", label)

    def test_both_sources_blocking_reports_the_broadest_cutoff(self):
        results = AnalysisResults(
            findings=[self._finding("MEDIUM")], block_merge_threshold="MEDIUM"
        )
        threshold = effective_block_threshold(results, ai_blocking=True)
        self.assertEqual(threshold, "MEDIUM")
        self.assertEqual(blocking_severity_label(threshold), "Medium-or-above")

    def test_ai_only_block_still_says_critical_high_even_on_a_medium_repo(self):
        results = AnalysisResults(
            findings=[self._finding("LOW")], block_merge_threshold="MEDIUM"
        )
        threshold = effective_block_threshold(results, ai_blocking=True)
        self.assertEqual(threshold, "HIGH")
        self.assertEqual(blocking_severity_label(threshold), "Critical/High")

    def test_nothing_blocking_returns_none_and_empty_label(self):
        results = AnalysisResults(findings=[self._finding("LOW")])
        threshold = effective_block_threshold(results, ai_blocking=False)
        self.assertIsNone(threshold)
        self.assertEqual(blocking_severity_label(threshold), "")

    def test_critical_and_low_thresholds_get_generic_accurate_wording(self):
        results = AnalysisResults(
            findings=[self._finding("CRITICAL")], block_merge_threshold="CRITICAL"
        )
        threshold = effective_block_threshold(results, ai_blocking=False)
        self.assertEqual(blocking_severity_label(threshold), "Critical")

        results = AnalysisResults(
            findings=[self._finding("LOW")], block_merge_threshold="LOW"
        )
        threshold = effective_block_threshold(results, ai_blocking=False)
        self.assertEqual(blocking_severity_label(threshold), "Low-or-above")

    def test_complexity_findings_still_never_trigger_a_threshold(self):
        results = AnalysisResults(
            findings=[self._finding("HIGH", category="complexity")],
            block_merge_threshold="MEDIUM",
        )
        threshold = effective_block_threshold(results, ai_blocking=False)
        self.assertIsNone(threshold)


class TestDetectLanguage(unittest.TestCase):
    """detect_language previously defaulted unconditionally to 'python' when
    no file matched a supported extension, silently mislabeling e.g. a
    markdown-only diff. Also covers the new javascript/typescript branches."""

    def test_dominant_language_among_mixed_files(self):
        self.assertEqual(detect_language(["a.ts", "b.ts", "c.py"]), "typescript")
        self.assertEqual(detect_language(["a.js", "b.jsx"]), "javascript")
        self.assertEqual(detect_language(["a.tsx", "b.py", "c.py"]), "python")

    def test_typescript_extensions_recognized(self):
        for ext in (".ts", ".tsx"):
            self.assertEqual(detect_language([f"a{ext}"]), "typescript")

    def test_javascript_extensions_recognized(self):
        for ext in (".js", ".jsx", ".mjs", ".cjs"):
            self.assertEqual(detect_language([f"a{ext}"]), "javascript")

    def test_unsupported_extension_returns_unknown_not_python(self):
        # Regression: this used to silently return "python" for a
        # completely unsupported extension list (e.g. a docs-only PR).
        self.assertEqual(detect_language(["README.md"]), "unknown")

    def test_empty_file_list_returns_unknown(self):
        self.assertEqual(detect_language([]), "unknown")


class TestRunEslint(unittest.TestCase):
    """run_eslint parses ESLint's --format json output into Finding objects,
    normalizes absolute filePath to project-relative (matching every other
    runner's path convention), and avoids the WARNING/error->HIGH
    over-blocking mistake already fixed for semgrep (see _semgrep_severity)
    -- except for eslint-plugin-security's own rules, which should still gate."""

    def test_empty_files_short_circuits(self):
        results = run_eslint([])
        self.assertEqual(results.findings, [])
        self.assertEqual(results.errors, [])

    def test_parses_findings_and_normalizes_absolute_path(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            payload = json.dumps([
                {
                    "filePath": str(Path(tmp_dir) / "src" / "app.ts"),
                    "messages": [
                        {"ruleId": "no-unused-vars", "severity": 2,
                         "message": "'x' is defined but never used.", "line": 10},
                        {"ruleId": "security/detect-eval-with-expression", "severity": 2,
                         "message": "eval with expression.", "line": 20},
                        {"ruleId": None, "severity": 1,
                         "message": "some warning with no ruleId.", "line": 30},
                    ],
                }
            ])

            def fake_run(cmd, cwd=None):
                return 0, payload, ""

            with mock.patch.object(static_analysis, "_run", side_effect=fake_run):
                results = run_eslint(["src/app.ts"], project_root=tmp_dir)

        self.assertEqual(results.errors, [])
        self.assertEqual(len(results.findings), 3)

        by_rule = {f.rule_id: f for f in results.findings}

        # Regular quality error: NOT escalated to HIGH (over-blocking fix).
        quality = by_rule["no-unused-vars"]
        self.assertEqual(quality.severity, config.SEVERITY_MEDIUM)
        self.assertEqual(quality.category, "quality")
        self.assertEqual(quality.file, str(Path("src") / "app.ts"))  # normalized, not absolute

        # eslint-plugin-security error: DOES gate at HIGH.
        security = by_rule["security/detect-eval-with-expression"]
        self.assertEqual(security.severity, config.SEVERITY_HIGH)
        self.assertEqual(security.category, "security")

        # Missing ruleId falls back to a stable placeholder, not a crash.
        placeholder = by_rule["eslint-error"]
        self.assertEqual(placeholder.severity, config.SEVERITY_LOW)  # warning, non-security

    def test_severity_overrides_applied(self):
        payload = json.dumps([
            {"filePath": "app.js", "messages": [
                {"ruleId": "no-console", "severity": 2, "message": "no console.", "line": 1},
            ]}
        ])

        def fake_run(cmd, cwd=None):
            return 0, payload, ""

        repo_config = mock.Mock(severity_overrides={"eslint": {"error": config.SEVERITY_LOW}})
        with mock.patch.object(static_analysis, "_run", side_effect=fake_run):
            results = run_eslint(["app.js"], repo_config=repo_config)

        self.assertEqual(results.findings[0].severity, config.SEVERITY_LOW)

    def test_tool_not_found_reports_error_not_crash(self):
        def fake_run(cmd, cwd=None):
            return -1, "", "Tool not found: eslint"

        with mock.patch.object(static_analysis, "_run", side_effect=fake_run):
            results = run_eslint(["app.js"])

        self.assertEqual(results.findings, [])
        self.assertIn("eslint", results.errors[0])


class TestRunTsc(unittest.TestCase):
    """run_tsc only runs when the target project has its own tsconfig.json
    (type-checking a single file with bare defaults produces noise, not
    signal), always compiles the whole project (types span files), and
    filters diagnostics back down to the changed files so a PR isn't blocked
    on pre-existing errors elsewhere in the codebase."""

    def test_no_tsconfig_is_silent_no_op(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            with mock.patch.object(static_analysis, "_run") as mock_run:
                results = run_tsc(["app.ts"], project_root=tmp_dir)
            mock_run.assert_not_called()

        self.assertEqual(results.findings, [])
        self.assertEqual(results.errors, [])

    def test_parses_diagnostics_and_filters_to_changed_files_only(self):
        tsc_output = (
            "src/app.ts(12,5): error TS2345: Argument of type 'string' is not assignable.\n"
            "src/unrelated.ts(3,1): error TS2304: Cannot find name 'Foo'.\n"
        )

        def fake_run(cmd, cwd=None):
            return 2, tsc_output, ""

        with tempfile.TemporaryDirectory() as tmp_dir:
            (Path(tmp_dir) / "tsconfig.json").write_text("{}")
            with mock.patch.object(static_analysis, "_run", side_effect=fake_run):
                results = run_tsc(["src/app.ts"], project_root=tmp_dir)

        # Only the finding in a file actually passed as `files` survives --
        # src/unrelated.ts must NOT block this PR.
        self.assertEqual(len(results.findings), 1)
        finding = results.findings[0]
        self.assertEqual(finding.tool, "tsc")
        self.assertEqual(finding.file, "src/app.ts")
        self.assertEqual(finding.line, 12)
        self.assertEqual(finding.rule_id, "TS2345")
        self.assertEqual(finding.severity, config.SEVERITY_HIGH)

    def test_tool_not_found_reports_error_not_crash(self):
        def fake_run(cmd, cwd=None):
            return -1, "", "Tool not found: tsc"

        with tempfile.TemporaryDirectory() as tmp_dir:
            (Path(tmp_dir) / "tsconfig.json").write_text("{}")
            with mock.patch.object(static_analysis, "_run", side_effect=fake_run):
                results = run_tsc(["app.ts"], project_root=tmp_dir)

        self.assertEqual(results.findings, [])
        self.assertIn("tsc", results.errors[0])


class TestPylintSeverityNoLongerOverblocks(unittest.TestCase):
    """H2 fix: pylint's broad 'E' (error) tier no longer maps to HIGH -- it
    includes ambient-environment-dependent findings (e.g. E0401 import-error
    when a dependency isn't installed in the scanning venv) alongside real
    bugs. 'F' (fatal -- the file couldn't even be parsed) is a narrower,
    genuinely severe tier and stays CRITICAL, unchanged."""

    def _run_pylint_with(self, msg_type):
        payload = json.dumps([{
            "type": msg_type, "path": "app.py", "line": 5,
            "message-id": "E0401" if msg_type == "error" else "X0000",
            "message": "Unable to import 'requests'",
        }])

        def fake_run(cmd, cwd=None):
            return 0, payload, ""

        with mock.patch.object(static_analysis, "_run", side_effect=fake_run):
            return run_pylint(["app.py"])

    def test_error_tier_downgraded_to_medium(self):
        results = self._run_pylint_with("error")
        self.assertEqual(results.findings[0].severity, config.SEVERITY_MEDIUM)

    def test_fatal_tier_still_critical(self):
        results = self._run_pylint_with("fatal")
        self.assertEqual(results.findings[0].severity, config.SEVERITY_CRITICAL)


class TestMypySeverityAmbientEnvCodes(unittest.TestCase):
    """H2 fix, mypy leg: import-not-found/import-untyped (mypy can't find the
    imported module/its stubs at all -- almost always an ambient-environment
    issue, the scanning venv lacking the target's real deps, not a genuine
    type-safety bug) are downgraded to MEDIUM. A real type mismatch is a
    genuine bug and stays HIGH -- this is NOT a blanket mypy severity
    downgrade, only these two specific, environment-dependent codes."""

    def _run_mypy_with_line(self, line):
        def fake_run(cmd, cwd=None):
            return 1, line, ""

        with mock.patch.object(static_analysis, "_run", side_effect=fake_run):
            return run_mypy(["app.py"])

    def test_import_not_found_downgraded_to_medium(self):
        results = self._run_mypy_with_line(
            "app.py:3:1: error: Cannot find implementation or library stub for module named \"requests\"  [import-not-found]"
        )
        self.assertEqual(len(results.findings), 1)
        self.assertEqual(results.findings[0].severity, config.SEVERITY_MEDIUM)
        self.assertEqual(results.findings[0].rule_id, "import-not-found")

    def test_import_untyped_downgraded_to_medium(self):
        results = self._run_mypy_with_line(
            "app.py:3:1: error: Library stubs not installed for \"yaml\"  [import-untyped]"
        )
        self.assertEqual(results.findings[0].severity, config.SEVERITY_MEDIUM)

    def test_real_type_error_still_high(self):
        results = self._run_mypy_with_line(
            'app.py:10:5: error: Argument 1 to "foo" has incompatible type "str"; expected "int"  [arg-type]'
        )
        self.assertEqual(results.findings[0].severity, config.SEVERITY_HIGH)
        self.assertEqual(results.findings[0].rule_id, "arg-type")

    def test_severity_override_still_wins_over_ambient_env_downgrade(self):
        repo_config = mock.Mock(severity_overrides={"mypy": {"error": config.SEVERITY_LOW}})

        def fake_run(cmd, cwd=None):
            return 1, "app.py:3:1: error: Cannot find module  [import-not-found]", ""

        with mock.patch.object(static_analysis, "_run", side_effect=fake_run):
            results = run_mypy(["app.py"], repo_config=repo_config)
        self.assertEqual(results.findings[0].severity, config.SEVERITY_LOW)


if __name__ == "__main__":
    unittest.main()
