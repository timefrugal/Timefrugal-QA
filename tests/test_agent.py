"""
Tests for qa_agent.agent's git-diff / file-discovery helpers.

Uses stdlib unittest (no pytest / test framework is set up in this repo yet),
following the convention established in tests/test_repo_config.py.
"""
import subprocess
import tempfile
import unittest
from pathlib import Path

from qa_agent.agent import get_changed_files, read_file_contents


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


if __name__ == "__main__":
    unittest.main()
