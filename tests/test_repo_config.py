"""
Tests for qa_agent.repo_config.

Uses stdlib unittest (no pytest / test framework is set up in this repo yet).
"""
import os
import tempfile
import unittest

from qa_agent.repo_config import RepoConfig, filter_ignored, load_repo_config
from qa_agent.static_analysis import Finding


class TestLoadRepoConfigMissingFile(unittest.TestCase):
    def test_missing_file_returns_defaults(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            # No .timefrugal-qa.yml written here at all.
            cfg = load_repo_config(tmpdir)
        self.assertEqual(cfg, RepoConfig())
        self.assertFalse(cfg.ai_blocking)
        self.assertIsNone(cfg.block_merge_threshold)
        self.assertEqual(cfg.severity_overrides, {})
        self.assertEqual(cfg.ignore, {})


class TestLoadRepoConfigMalformedYaml(unittest.TestCase):
    def test_malformed_yaml_returns_defaults_without_raising(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, ".timefrugal-qa.yml")
            with open(path, "w") as f:
                # Invalid YAML: unbalanced flow mapping.
                f.write("ai: {blocking: true\n  garbage: [1, 2\n")
            cfg = load_repo_config(tmpdir)  # must not raise
        self.assertEqual(cfg, RepoConfig())

    def test_empty_file_returns_defaults(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, ".timefrugal-qa.yml")
            with open(path, "w") as f:
                f.write("")
            cfg = load_repo_config(tmpdir)
        self.assertEqual(cfg, RepoConfig())

    def test_yaml_that_parses_to_non_dict_returns_defaults(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, ".timefrugal-qa.yml")
            with open(path, "w") as f:
                f.write("- just\n- a\n- list\n")
            cfg = load_repo_config(tmpdir)
        self.assertEqual(cfg, RepoConfig())


class TestLoadRepoConfigFullyPopulated(unittest.TestCase):
    def test_fully_populated_yaml_parses_every_field(self):
        yaml_text = """
ai:
  blocking: true
block_merge_threshold: CRITICAL
severity_overrides:
  pylint:
    E: MEDIUM
  mypy:
    error: MEDIUM
ignore:
  bandit:
    - B101
    - B105
  pip_audit:
    - GHSA-xxxx-yyyy-zzzz
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, ".timefrugal-qa.yml")
            with open(path, "w") as f:
                f.write(yaml_text)
            cfg = load_repo_config(tmpdir)

        self.assertTrue(cfg.ai_blocking)
        self.assertEqual(cfg.block_merge_threshold, "CRITICAL")
        self.assertEqual(cfg.severity_overrides, {
            "pylint": {"E": "MEDIUM"},
            "mypy": {"error": "MEDIUM"},
        })
        self.assertEqual(cfg.ignore, {
            "bandit": ["B101", "B105"],
            "pip_audit": ["GHSA-xxxx-yyyy-zzzz"],
        })

    def test_partial_yaml_fills_remaining_fields_with_defaults(self):
        yaml_text = "ai:\n  blocking: true\n"
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, ".timefrugal-qa.yml")
            with open(path, "w") as f:
                f.write(yaml_text)
            cfg = load_repo_config(tmpdir)

        self.assertTrue(cfg.ai_blocking)
        self.assertIsNone(cfg.block_merge_threshold)
        self.assertEqual(cfg.severity_overrides, {})
        self.assertEqual(cfg.ignore, {})


class TestLoadRepoConfigMalformedFieldTypes(unittest.TestCase):
    """Regression coverage for fields that parse fine at the top level but are
    the wrong type underneath (e.g. `ai: "yes"` instead of `ai: {...}`).
    Each of these must fall back to that field's default, not raise."""

    def test_ai_as_string_falls_back_to_default_without_raising(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, ".timefrugal-qa.yml")
            with open(path, "w") as f:
                f.write('ai: "yes"\n')
            cfg = load_repo_config(tmpdir)  # must not raise
        self.assertEqual(cfg, RepoConfig())
        self.assertFalse(cfg.ai_blocking)

    def test_ignore_as_string_falls_back_to_empty_dict_without_raising(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, ".timefrugal-qa.yml")
            with open(path, "w") as f:
                f.write('ignore: "bogus"\n')
            cfg = load_repo_config(tmpdir)  # must not raise
        self.assertEqual(cfg.ignore, {})

    def test_severity_overrides_as_string_falls_back_to_empty_dict_without_raising(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, ".timefrugal-qa.yml")
            with open(path, "w") as f:
                f.write('severity_overrides: "not a dict"\n')
            cfg = load_repo_config(tmpdir)  # must not raise
        self.assertEqual(cfg.severity_overrides, {})


class TestFilterIgnored(unittest.TestCase):
    def _finding(self, tool, rule_id, severity="HIGH", category="security"):
        return Finding(
            tool=tool,
            severity=severity,
            category=category,
            file="app.py",
            line=1,
            message="msg",
            rule_id=rule_id,
        )

    def test_removes_matching_tool_and_rule_id(self):
        findings = [
            self._finding("bandit", "B101"),
            self._finding("bandit", "B105"),
            self._finding("bandit", "B608"),
        ]
        ignore_map = {"bandit": ["B101", "B105"]}
        out = filter_ignored(findings, ignore_map)
        self.assertEqual([f.rule_id for f in out], ["B608"])

    def test_leaves_non_matching_findings_untouched(self):
        findings = [
            self._finding("bandit", "B101"),
            self._finding("pylint", "E0001"),
        ]
        ignore_map = {"semgrep": ["some-rule"]}
        out = filter_ignored(findings, ignore_map)
        self.assertEqual(out, findings)

    def test_empty_ignore_map_returns_findings_unchanged(self):
        findings = [self._finding("bandit", "B101")]
        out = filter_ignored(findings, {})
        self.assertEqual(out, findings)

    def test_non_dict_ignore_map_returns_findings_unchanged_without_raising(self):
        # Belt-and-suspenders: load_repo_config should never let a non-dict
        # `ignore` reach here, but filter_ignored guards against it directly
        # too, in case it's ever called from somewhere else.
        findings = [self._finding("bandit", "B101")]
        out = filter_ignored(findings, "bogus")  # must not raise
        self.assertEqual(out, findings)

    def test_normalizes_hyphenated_tool_name_to_underscore(self):
        findings = [
            self._finding("pip-audit", "GHSA-xxxx-yyyy-zzzz", category="dependency"),
        ]
        ignore_map = {"pip_audit": ["GHSA-xxxx-yyyy-zzzz"]}
        out = filter_ignored(findings, ignore_map)
        self.assertEqual(out, [])

    def test_only_matches_same_tool_not_just_rule_id(self):
        findings = [
            self._finding("pylint", "B101"),  # same rule_id, different tool
        ]
        ignore_map = {"bandit": ["B101"]}
        out = filter_ignored(findings, ignore_map)
        self.assertEqual(out, findings)

    def test_non_list_per_tool_ignore_value_int_does_not_raise(self):
        # e.g. `ignore: {bandit: 101}` -- a plausible typo forgetting the
        # brackets around a single rule ID. Must not raise TypeError on the
        # `f.rule_id in ignored_ids` containment check; the finding should
        # simply not be filtered.
        findings = [self._finding("bandit", "B101")]
        out = filter_ignored(findings, {"bandit": 101})  # must not raise
        self.assertEqual(out, findings)

    def test_non_list_per_tool_ignore_value_none_does_not_raise(self):
        # e.g. `ignore: {bandit: null}` (empty YAML value under a key).
        findings = [self._finding("bandit", "B101")]
        out = filter_ignored(findings, {"bandit": None})  # must not raise
        self.assertEqual(out, findings)

    def test_non_list_per_tool_ignore_value_float_does_not_raise(self):
        findings = [self._finding("bandit", "B101")]
        out = filter_ignored(findings, {"bandit": 3.14})  # must not raise
        self.assertEqual(out, findings)

    def test_non_list_per_tool_ignore_value_bool_does_not_raise(self):
        findings = [self._finding("bandit", "B101")]
        out = filter_ignored(findings, {"bandit": True})  # must not raise
        self.assertEqual(out, findings)

    def test_non_list_per_tool_ignore_value_does_not_affect_other_tools(self):
        # A bad value for one tool shouldn't stop a valid list from working
        # for a different tool in the same ignore map.
        findings = [
            self._finding("bandit", "B101"),
            self._finding("pylint", "E0001"),
        ]
        ignore_map = {"bandit": 101, "pylint": ["E0001"]}
        out = filter_ignored(findings, ignore_map)  # must not raise
        self.assertEqual([f.rule_id for f in out], ["B101"])


if __name__ == "__main__":
    unittest.main()
