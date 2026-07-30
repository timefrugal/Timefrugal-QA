"""
Tests for qa_agent.ai_review.

Uses stdlib unittest (no pytest / test framework is set up in this repo yet),
following the convention established in tests/test_repo_config.py.
"""
import unittest
from unittest import mock

from qa_agent import config
from qa_agent.ai_review import _per_file_char_budget, _validate_severity


class TestValidateSeverityRejectsInvalidAndMissingValues(unittest.TestCase):
    """
    H1: the AI's JSON response is untrusted input -- a hallucinated, wrong-case,
    or missing severity string must never be trusted outright. _validate_severity
    is the single chokepoint that guarantees every AIFinding.severity is one of
    config.SEVERITY_ORDER, falling back to config.SEVERITY_INFO otherwise.
    """

    def test_valid_severity_passes_through_unchanged(self):
        self.assertEqual(_validate_severity("CRITICAL"), "CRITICAL")
        self.assertEqual(_validate_severity("HIGH"), "HIGH")
        self.assertEqual(_validate_severity("MEDIUM"), "MEDIUM")
        self.assertEqual(_validate_severity("LOW"), "LOW")
        self.assertEqual(_validate_severity("INFO"), "INFO")

    def test_lowercase_valid_value_is_normalized_to_uppercase(self):
        # The real implementation upper()s before checking membership, so it
        # is case-INsensitive -- a lowercase valid severity is accepted and
        # normalized, not rejected. Testing actual behavior, not an assumption.
        self.assertEqual(_validate_severity("high"), "HIGH")
        self.assertEqual(_validate_severity("Critical"), "CRITICAL")
        self.assertEqual(_validate_severity("mEdIuM"), "MEDIUM")

    def test_garbage_string_falls_back_to_info(self):
        self.assertEqual(_validate_severity("BOGUS"), config.SEVERITY_INFO)
        self.assertEqual(_validate_severity("super-duper-critical"), config.SEVERITY_INFO)

    def test_none_falls_back_to_info(self):
        self.assertEqual(_validate_severity(None), config.SEVERITY_INFO)

    def test_empty_string_falls_back_to_info(self):
        self.assertEqual(_validate_severity(""), config.SEVERITY_INFO)

    def test_whitespace_only_string_falls_back_to_info(self):
        self.assertEqual(_validate_severity("   "), config.SEVERITY_INFO)

    def test_surrounding_whitespace_is_stripped_on_valid_value(self):
        self.assertEqual(_validate_severity("  HIGH  "), "HIGH")


class TestPerFileCharBudgetKeepsTotalPromptSizeBounded(unittest.TestCase):
    """
    Regression coverage: a fixed per-file truncation cap scales unboundedly
    with file count -- a 9-file PR requested ~15,500 tokens against Groq's
    free-tier 12,000 TPM limit and got a 413. _per_file_char_budget spreads
    a total character budget (config.AI_MAX_TOTAL_CONTENT_CHARS) across
    however many files changed, so total prompt size stays roughly bounded
    regardless of file count.
    """

    def test_few_files_keeps_original_per_file_cap(self):
        # 1-2 files: well under the total budget, so the original fixed cap
        # (matches prior single-file behavior) should win, not the split.
        with mock.patch.object(config, "AI_MAX_TOTAL_CONTENT_CHARS", 16000):
            self.assertEqual(_per_file_char_budget(1, per_file_cap=6000), 6000)
            self.assertEqual(_per_file_char_budget(2, per_file_cap=6000), 6000)

    def test_many_files_splits_budget_and_stays_bounded(self):
        with mock.patch.object(config, "AI_MAX_TOTAL_CONTENT_CHARS", 16000):
            budget = _per_file_char_budget(9, per_file_cap=6000)
            self.assertLess(budget, 6000)
            # Total across all files must not exceed the configured budget.
            self.assertLessEqual(budget * 9, 16000)

    def test_very_many_files_never_goes_below_floor(self):
        with mock.patch.object(config, "AI_MAX_TOTAL_CONTENT_CHARS", 16000):
            budget = _per_file_char_budget(500, per_file_cap=6000, floor=500)
            self.assertEqual(budget, 500)

    def test_zero_files_returns_the_cap_unchanged(self):
        # Defensive: review_code/generate_tests both bail out earlier on
        # empty file_contents, but this must not raise (division by zero).
        self.assertEqual(_per_file_char_budget(0, per_file_cap=6000), 6000)


if __name__ == "__main__":
    unittest.main()
