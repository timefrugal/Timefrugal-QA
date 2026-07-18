"""
Tests for qa_agent.static_analysis.

Uses stdlib unittest (no pytest / test framework is set up in this repo yet),
following the convention established in tests/test_repo_config.py and
tests/test_agent.py.
"""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from qa_agent import static_analysis
from qa_agent.static_analysis import AnalysisResults, Finding, run_all, run_pip_audit


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


if __name__ == "__main__":
    unittest.main()
