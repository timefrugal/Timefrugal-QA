"""
Tests for qa_agent.pr_reporter.

Uses stdlib unittest (no pytest / test framework is set up in this repo yet),
following the convention established in tests/test_repo_config.py.
"""
import unittest
from unittest import mock

from qa_agent import config, pr_reporter


class _FakeResponse:
    def __init__(self, status_code=201):
        self.status_code = status_code
        self.text = ""

    def json(self):
        return {}


class TestSetCommitStatusPrecedence(unittest.TestCase):
    """
    C1 regression: set_commit_status's state computation must give `blocked`
    strict priority over `errored`. The bug this guards against: an earlier
    version computed state such that `errored=True` could produce GitHub
    status "error" even when `blocked=True`, which is the wrong signal --
    "failure" (a real blocking finding) must never be downgraded/reclassified
    to "error" (tool trouble) just because a tool also failed to run. This was
    previously only checked with an ad hoc manual script; this is the
    permanent regression test.
    """

    def setUp(self):
        # set_commit_status short-circuits (returns False, no HTTP call) unless
        # GITHUB_REPOSITORY and GITHUB_SHA are set -- patch config directly
        # rather than mutating the environment.
        patcher_repo = mock.patch.object(config, "GITHUB_REPOSITORY", "owner/repo")
        patcher_sha = mock.patch.object(config, "GITHUB_SHA", "deadbeef")
        patcher_repo.start()
        patcher_sha.start()
        self.addCleanup(patcher_repo.stop)
        self.addCleanup(patcher_sha.stop)

    def _post_and_capture_state(self, blocked, errored):
        captured = {}

        def fake_post(url, headers=None, json=None, timeout=None):
            captured["payload"] = json
            return _FakeResponse(201)

        with mock.patch("qa_agent.pr_reporter.requests.post", side_effect=fake_post):
            ok = pr_reporter.set_commit_status(blocked=blocked, errored=errored)

        self.assertTrue(ok)
        return captured["payload"]["state"]

    def test_blocked_takes_priority_over_errored_in_commit_status(self):
        state = self._post_and_capture_state(blocked=True, errored=True)
        self.assertEqual(state, "failure")
        self.assertNotEqual(state, "error")

    def test_blocked_alone_is_failure(self):
        state = self._post_and_capture_state(blocked=True, errored=False)
        self.assertEqual(state, "failure")

    def test_errored_alone_is_error(self):
        state = self._post_and_capture_state(blocked=False, errored=True)
        self.assertEqual(state, "error")

    def test_neither_is_success(self):
        state = self._post_and_capture_state(blocked=False, errored=False)
        self.assertEqual(state, "success")


if __name__ == "__main__":
    unittest.main()
