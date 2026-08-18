"""
Tests for qa_agent.agent's git-diff / file-discovery helpers.

Uses stdlib unittest (no pytest / test framework is set up in this repo yet),
following the convention established in tests/test_repo_config.py.
"""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from qa_agent.agent import (
    get_changed_files,
    get_changed_line_ranges,
    get_diff_text,
    read_file_contents,
)


def _git(repo_dir, *args):
    subprocess.run(
        ["git", *args],
        cwd=repo_dir, check=True, capture_output=True, text=True,
    )


def _init_repo(repo_dir):
    _git(repo_dir, "init", "-q")
    _git(repo_dir, "config", "user.email", "test@example.com")
    _git(repo_dir, "config", "user.name", "test")


class TestGetChangedFilesThreeDotDiff(unittest.TestCase):
    """
    M1: get_changed_files must use three-dot diff semantics (base_ref...HEAD),
    i.e. diff against the merge-base, not against base_ref's current tip.
    A file changed only on the base branch *after* the feature branch
    diverged must never show up as a "changed file" for the feature branch.
    """

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.repo = self._tmpdir.name
        self.addCleanup(self._tmpdir.cleanup)

        _init_repo(self.repo)

        # Initial commit on main, with a file shared by both branches.
        (Path(self.repo) / "shared.py").write_text("print('v1')\n")
        _git(self.repo, "add", "shared.py")
        _git(self.repo, "commit", "-q", "-m", "initial commit")
        _git(self.repo, "branch", "-m", "main")

        # Diverge: feature branch adds its own file.
        _git(self.repo, "checkout", "-q", "-b", "feature")
        (Path(self.repo) / "feature.py").write_text("print('feature')\n")
        _git(self.repo, "add", "feature.py")
        _git(self.repo, "commit", "-q", "-m", "add feature.py")

        # Move main forward with a change the feature branch never saw.
        _git(self.repo, "checkout", "-q", "main")
        (Path(self.repo) / "shared.py").write_text("print('v2 - changed after divergence')\n")
        _git(self.repo, "add", "shared.py")
        _git(self.repo, "commit", "-q", "-m", "modify shared.py on main after divergence")

        _git(self.repo, "checkout", "-q", "feature")

    def test_excludes_files_only_changed_on_base_since_divergence(self):
        files = get_changed_files(base_ref="main", project_root=self.repo)
        self.assertEqual(files, ["feature.py"])
        self.assertNotIn("shared.py", files)

    def test_two_dot_diff_would_have_incorrectly_included_it(self):
        # Sanity check the fixture itself demonstrates the bug being fixed:
        # raw two-dot diff (the pre-fix command shape) DOES pick up shared.py.
        result = subprocess.run(
            ["git", "diff", "--name-only", "--diff-filter=ACMR", "main", "HEAD"],
            cwd=self.repo, capture_output=True, text=True, check=True,
        )
        two_dot_files = set(result.stdout.split())
        self.assertIn("shared.py", two_dot_files)


class TestGetChangedFilesProjectRoot(unittest.TestCase):
    """M1: get_changed_files must operate against project_root, not cwd."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.repo = self._tmpdir.name
        self.addCleanup(self._tmpdir.cleanup)

        _init_repo(self.repo)
        (Path(self.repo) / "base.py").write_text("print('base')\n")
        _git(self.repo, "add", "base.py")
        _git(self.repo, "commit", "-q", "-m", "initial commit")
        _git(self.repo, "branch", "-m", "main")

        _git(self.repo, "checkout", "-q", "-b", "feature")
        src_dir = Path(self.repo) / "src"
        src_dir.mkdir()
        (src_dir / "new_module.py").write_text("def f():\n    return 1\n")
        _git(self.repo, "add", "src/new_module.py")
        _git(self.repo, "commit", "-q", "-m", "add src/new_module.py")

    def test_operates_against_project_root_not_cwd(self):
        # cwd for the test process is wherever unittest was invoked from,
        # which is not self.repo -- the whole point is that project_root
        # must be threaded through rather than relying on cwd.
        files = get_changed_files(base_ref="main", project_root=self.repo)
        self.assertEqual(files, ["src/new_module.py"])

    def test_read_file_contents_resolves_relative_to_project_root(self):
        files = get_changed_files(base_ref="main", project_root=self.repo)
        contents = read_file_contents(files, project_root=self.repo)
        self.assertIn("src/new_module.py", contents)
        self.assertIn("return 1", contents["src/new_module.py"])


class TestGetChangedLineRanges(unittest.TestCase):
    """
    jarvis-infra issues #306/#307/#310: the AI reviewer had no way to tell
    newly-introduced lines from code that predates a PR by months, and
    routinely fabricated/escalated CRITICAL/HIGH findings against
    pre-existing code as a result. get_changed_line_ranges() is the
    ground-truth source review_code()'s severity-demotion pass checks
    against -- these tests verify it against real `git diff` output, not
    a re-implementation of diff parsing.
    """

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.repo = self._tmpdir.name
        self.addCleanup(self._tmpdir.cleanup)
        _init_repo(self.repo)

    def _commit(self, filename, content, message):
        (Path(self.repo) / filename).write_text(content)
        _git(self.repo, "add", filename)
        _git(self.repo, "commit", "-q", "-m", message)

    def test_single_added_line_is_the_only_line_in_range(self):
        self._commit("app.py", "line1\nline2\nline3\n", "initial")
        _git(self.repo, "branch", "-m", "main")
        _git(self.repo, "checkout", "-q", "-b", "feature")
        self._commit("app.py", "line1\nline2\nline3\nline4\n", "add a line")

        ranges = get_changed_line_ranges("main", ["app.py"], project_root=self.repo)
        self.assertEqual(ranges, {"app.py": {4}})

    def test_modified_line_in_the_middle_is_the_only_line_in_range(self):
        self._commit("app.py", "line1\nline2\nline3\n", "initial")
        _git(self.repo, "branch", "-m", "main")
        _git(self.repo, "checkout", "-q", "-b", "feature")
        self._commit("app.py", "line1\nCHANGED\nline3\n", "modify line 2")

        ranges = get_changed_line_ranges("main", ["app.py"], project_root=self.repo)
        self.assertEqual(ranges, {"app.py": {2}})

    def test_pure_deletion_contributes_no_lines_but_file_still_present(self):
        self._commit("app.py", "line1\nline2\nline3\n", "initial")
        _git(self.repo, "branch", "-m", "main")
        _git(self.repo, "checkout", "-q", "-b", "feature")
        self._commit("app.py", "line1\nline3\n", "delete line 2")

        ranges = get_changed_line_ranges("main", ["app.py"], project_root=self.repo)
        # The file legitimately appears (it's a changed file), but a pure
        # deletion adds nothing to the new-file line range -- there's no
        # added/modified line left to scope a finding against.
        self.assertIn("app.py", ranges)
        self.assertEqual(ranges["app.py"], set())

    def test_multiple_hunks_in_one_file_both_contribute(self):
        lines = [f"line{i}\n" for i in range(1, 21)]
        self._commit("app.py", "".join(lines), "initial")
        _git(self.repo, "branch", "-m", "main")
        _git(self.repo, "checkout", "-q", "-b", "feature")
        lines[1] = "CHANGED_NEAR_TOP\n"     # new-file line 2
        lines[17] = "CHANGED_NEAR_BOTTOM\n"  # new-file line 18
        self._commit("app.py", "".join(lines), "two separate edits")

        ranges = get_changed_line_ranges("main", ["app.py"], project_root=self.repo)
        self.assertEqual(ranges["app.py"], {2, 18})

    def test_unrelated_file_not_in_the_diff_is_absent_from_ranges(self):
        self._commit("app.py", "line1\n", "initial")
        self._commit("other.py", "print('untouched')\n", "add other.py")
        _git(self.repo, "branch", "-m", "main")
        _git(self.repo, "checkout", "-q", "-b", "feature")
        self._commit("app.py", "line1\nline2\n", "modify app.py only")

        ranges = get_changed_line_ranges("main", ["app.py"], project_root=self.repo)
        self.assertNotIn("other.py", ranges)

    def test_no_files_returns_empty_dict(self):
        self.assertEqual(get_changed_line_ranges("main", [], project_root=self.repo), {})


class TestGetDiffText(unittest.TestCase):
    """get_diff_text() must return a real, non-empty unified diff for an
    actual change, and empty string for no files -- the direct input to
    review_code()'s new diff_text prompt section."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.repo = self._tmpdir.name
        self.addCleanup(self._tmpdir.cleanup)
        _init_repo(self.repo)
        (Path(self.repo) / "app.py").write_text("line1\nline2\n")
        _git(self.repo, "add", "app.py")
        _git(self.repo, "commit", "-q", "-m", "initial")
        _git(self.repo, "branch", "-m", "main")
        _git(self.repo, "checkout", "-q", "-b", "feature")
        (Path(self.repo) / "app.py").write_text("line1\nline2\nline3\n")
        _git(self.repo, "add", "app.py")
        _git(self.repo, "commit", "-q", "-m", "add line3")

    def test_returns_a_real_unified_diff_containing_the_new_line(self):
        diff = get_diff_text("main", ["app.py"], project_root=self.repo)
        self.assertIn("+line3", diff)
        self.assertIn("app.py", diff)

    def test_no_files_returns_empty_string(self):
        self.assertEqual(get_diff_text("main", [], project_root=self.repo), "")


class TestMainForcesLineBufferedStdout(unittest.TestCase):
    """
    jarvis-infra#286: under GitHub Actions, stdout is not a TTY, so Python
    fully block-buffers it -- every real-time [agent] progress print
    silently accumulates and only appears in one burst right as the process
    exits, which was repeatedly misread as a hung/crashed CI run when the
    run had actually always completed successfully. Importing qa_agent.__main__
    must force line buffering so progress streams in real time even when
    stdout is piped (non-tty), which is exactly the condition under test
    here via subprocess capture_output=True.
    """

    def test_stdout_is_line_buffered_when_piped(self):
        result = subprocess.run(
            [sys.executable, "-c", "import qa_agent.__main__, sys; print(sys.stdout.line_buffering)"],
            capture_output=True, text=True, check=True,
        )
        self.assertEqual(result.stdout.strip(), "True")


if __name__ == "__main__":
    unittest.main()
