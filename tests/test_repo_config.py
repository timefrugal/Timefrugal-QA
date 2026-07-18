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


class TestLoadRepoConfigBlockMergeThreshold(unittest.TestCase):
    """Regression coverage for the round-3 finding: `block_merge_threshold`
    had zero validation, so a typo or wrong-type value crashed the entire
    gate via `threshold_order.index(threshold)` raising ValueError -- and
    unlike the other fields, nothing in the call chain caught it."""

    def _load(self, yaml_text):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, ".timefrugal-qa.yml")
            with open(path, "w") as f:
                f.write(yaml_text)
            return load_repo_config(tmpdir)

    def test_typo_value_falls_back_to_none_without_raising(self):
        cfg = self._load("block_merge_threshold: CRIT\n")  # must not raise
        self.assertIsNone(cfg.block_merge_threshold)

    def test_wrong_case_falls_back_to_none_without_raising(self):
        # Case matters: the codebase's severities are all upper-case
        # constants, matched with exact equality/list membership elsewhere
        # (e.g. threshold_order.index()), so lower/mixed case is invalid.
        cfg = self._load("block_merge_threshold: high\n")  # must not raise
        self.assertIsNone(cfg.block_merge_threshold)

    def test_wrong_type_int_falls_back_to_none_without_raising(self):
        cfg = self._load("block_merge_threshold: 5\n")  # must not raise
        self.assertIsNone(cfg.block_merge_threshold)

    def test_wrong_type_list_falls_back_to_none_without_raising(self):
        cfg = self._load("block_merge_threshold: [HIGH]\n")  # must not raise
        self.assertIsNone(cfg.block_merge_threshold)

    def test_valid_value_is_preserved(self):
        cfg = self._load("block_merge_threshold: HIGH\n")
        self.assertEqual(cfg.block_merge_threshold, "HIGH")

    def test_absent_value_is_none(self):
        cfg = self._load("ai:\n  blocking: true\n")
        self.assertIsNone(cfg.block_merge_threshold)


class TestLoadRepoConfigSeverityOverrideValues(unittest.TestCase):
    """Regression coverage for a second instance of the same bug class found
    during the round-4 comprehensive audit: severity_overrides' per-tool
    values (e.g. `pylint: {F: BOGUS}`) were never validated. That doesn't
    crash inside repo_config.py or static_analysis.py (has_blocking_issues
    only does list membership), but it silently produces a Finding with an
    invalid severity string that later crashes ai_review._format_static_for_ai
    (and would also crash local_reporter's sort) via a `.index()` lookup that
    has no containment of its own -- an uncaught crash reachable from a
    normal review run, not just a hypothetical."""

    def _load(self, yaml_text):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, ".timefrugal-qa.yml")
            with open(path, "w") as f:
                f.write(yaml_text)
            return load_repo_config(tmpdir)

    def test_invalid_leaf_severity_dropped_without_raising(self):
        cfg = self._load(
            "severity_overrides:\n  pylint:\n    F: BOGUS\n"
        )  # must not raise
        self.assertEqual(cfg.severity_overrides, {"pylint": {}})

    def test_invalid_leaf_severity_does_not_remove_valid_siblings(self):
        cfg = self._load(
            "severity_overrides:\n  pylint:\n    F: BOGUS\n    E: MEDIUM\n"
        )
        self.assertEqual(cfg.severity_overrides, {"pylint": {"E": "MEDIUM"}})

    def test_wrong_case_leaf_severity_dropped_without_raising(self):
        cfg = self._load(
            "severity_overrides:\n  mypy:\n    error: high\n"
        )  # lower-case "high" is invalid, must not raise
        self.assertEqual(cfg.severity_overrides, {"mypy": {}})

    def test_non_string_leaf_severity_dropped_without_raising(self):
        cfg = self._load(
            "severity_overrides:\n  pylint:\n    F: 5\n"
        )  # must not raise
        self.assertEqual(cfg.severity_overrides, {"pylint": {}})

    def test_non_dict_per_tool_value_dropped_without_raising(self):
        cfg = self._load(
            "severity_overrides:\n  pylint: not_a_mapping\n"
        )  # must not raise
        self.assertEqual(cfg.severity_overrides, {"pylint": {}})

    def test_valid_leaf_severities_preserved(self):
        cfg = self._load(
            "severity_overrides:\n  pylint:\n    F: CRITICAL\n    E: LOW\n"
        )
        self.assertEqual(cfg.severity_overrides, {"pylint": {"F": "CRITICAL", "E": "LOW"}})


class TestLoadRepoConfigAllFieldsMalformedAtOnce(unittest.TestCase):
    """Stronger regression guard than testing each field in isolation: every
    field wrong-typed/invalid simultaneously in one file must still load to
    something functionally identical to RepoConfig() (all defaults), with no
    exception anywhere in the chain."""

    def test_every_field_malformed_still_yields_effective_defaults(self):
        yaml_text = """
ai: "yes"
block_merge_threshold: CRIT
severity_overrides:
  pylint:
    F: BOGUS
    E: 5
  mypy: "not_a_mapping"
  semgrep:
    WARNING: low
ignore: "bogus"
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, ".timefrugal-qa.yml")
            with open(path, "w") as f:
                f.write(yaml_text)
            cfg = load_repo_config(tmpdir)  # must not raise

        # ai_blocking: 'ai' isn't a mapping -> default False.
        self.assertFalse(cfg.ai_blocking)
        # block_merge_threshold: invalid -> None (use config.py's default).
        self.assertIsNone(cfg.block_merge_threshold)
        # severity_overrides: every leaf was invalid or non-mapping -> all
        # tool entries present but empty (functionally identical to {} for
        # every .get(tool, {}) call site in static_analysis.py).
        self.assertEqual(
            cfg.severity_overrides,
            {"pylint": {}, "mypy": {}, "semgrep": {}},
        )
        for tool_overrides in cfg.severity_overrides.values():
            self.assertEqual(tool_overrides, {})
        # ignore: not a mapping -> default {}.
        self.assertEqual(cfg.ignore, {})

        # Functionally identical to RepoConfig() for every consumer:
        # ai_blocking False, threshold None, ignore {} (filter_ignored is a
        # no-op on {}), and every severity_overrides.get(tool, {}) call
        # returns {} same as it would against RepoConfig()'s empty dict.
        self.assertFalse(cfg.ai_blocking)
        self.assertIsNone(cfg.block_merge_threshold)
        self.assertEqual(cfg.ignore, {})
        self.assertEqual(cfg.severity_overrides.get("pylint", {}), {})
        self.assertEqual(cfg.severity_overrides.get("mypy", {}), {})
        self.assertEqual(cfg.severity_overrides.get("nonexistent", {}), {})


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
