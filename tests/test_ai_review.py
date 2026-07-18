"""
Tests for qa_agent.ai_review.

Uses stdlib unittest (no pytest / test framework is set up in this repo yet),
following the convention established in tests/test_repo_config.py.
"""
import unittest

from qa_agent import config
from qa_agent.ai_review import _validate_severity


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


if __name__ == "__main__":
    unittest.main()
