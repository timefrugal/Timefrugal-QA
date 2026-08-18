"""
Timefrugal-QA Agent — main orchestrator.
Determines changed files, runs all analysis, generates tests, then reports.
"""
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Dict, List, Optional, Set

from qa_agent import config
from qa_agent.static_analysis import run_all, detect_language, AnalysisResults
from qa_agent.ai_review import review_code, generate_tests
from qa_agent.repo_config import load_repo_config


# ──────────────────────────────────────────────
# Git utilities
# ──────────────────────────────────────────────

def get_changed_files(base_ref: str = "origin/main", project_root: str = ".") -> List[str]:
    """Return list of changed Python, Java, and HTML files compared to base_ref.

    Uses three-dot diff semantics (base_ref...HEAD), i.e. diffs HEAD against
    the merge-base of base_ref and HEAD, not against base_ref's current tip.
    This matters on a stale local branch: two-dot diff (base_ref HEAD as
    separate args) would also report files that only changed on base_ref
    since the branch diverged, which is not "changed files in this PR".
    """
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", "--diff-filter=ACMR", f"{base_ref}...HEAD"],
            capture_output=True, text=True, check=True, cwd=project_root,
        )
        files = [
            f.strip() for f in result.stdout.splitlines()
            if Path(f.strip()).suffix.lower() in config.SUPPORTED_EXTENSIONS
            and (Path(project_root) / f.strip()).exists()
            and not any(excl in f for excl in config.EXCLUDE_PATTERNS)
        ]
        return files
    except subprocess.CalledProcessError as e:
        print(f"[agent] git diff failed: {e.stderr.strip()}", file=sys.stderr)
        return []


def get_diff_text(base_ref: str, files: List[str], project_root: str = ".",
                   context_lines: int = 3) -> str:
    """Return the unified diff text (default 3 lines of context) for the
    given files against base_ref -- the actual patch, not full file
    content. Same three-dot semantics as get_changed_files() above, for
    the same stale-local-branch reason.

    Sent to the AI reviewer (review_code()'s diff_text param) alongside
    the existing full-file content, so it has an actual diff boundary to
    work from -- see get_changed_line_ranges() below for the structural
    (non-prompt-dependent) half of this fix."""
    if not files:
        return ""
    try:
        result = subprocess.run(
            ["git", "diff", f"--unified={context_lines}", f"{base_ref}...HEAD", "--", *files],
            capture_output=True, text=True, check=True, cwd=project_root,
        )
        return result.stdout
    except subprocess.CalledProcessError as e:
        print(f"[agent] git diff (patch text) failed: {e.stderr.strip()}", file=sys.stderr)
        return ""


_HUNK_HEADER_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def get_changed_line_ranges(base_ref: str, files: List[str],
                             project_root: str = ".") -> Dict[str, Set[int]]:
    """Return, per file, the set of new-file line numbers actually
    touched (added or modified) by the diff against base_ref -- parsed
    from unified diff hunk headers (`@@ -a,b +c,d @@`; the `+c,d` part is
    the new-file line range for that hunk), independent of what content
    was actually sent to any AI provider.

    Uses --unified=0 (zero context lines) specifically so every reported
    line in a hunk is a genuinely added/modified line, not a context line
    that would otherwise pollute the range -- unlike get_diff_text() above,
    which deliberately keeps context for human/AI readability.

    This is the STRUCTURAL half of the diff-scoping fix (see review_code()'s
    severity-demotion pass): a prompt instruction alone is not reliable
    enough to keep a small/free-tier model from flagging pre-existing code
    at CRITICAL/HIGH severity (jarvis-infra issues #306/#307/#310, all
    fabricated or escalated findings against code outside the diff despite
    already being asked for a "thorough" review) -- this function gives
    review_code() a ground-truth answer to check the AI's claims against,
    computed the same way regardless of how well any given provider follows
    instructions.

    A hunk with a zero new-line count (`+c,0`, a pure deletion at that
    position) contributes nothing to the range -- there's no added/modified
    line to flag there."""
    ranges: Dict[str, Set[int]] = {}
    if not files:
        return ranges
    try:
        result = subprocess.run(
            ["git", "diff", "--unified=0", f"{base_ref}...HEAD", "--", *files],
            capture_output=True, text=True, check=True, cwd=project_root,
        )
    except subprocess.CalledProcessError as e:
        print(f"[agent] git diff (line ranges) failed: {e.stderr.strip()}", file=sys.stderr)
        return ranges

    current_file = None
    for line in result.stdout.splitlines():
        if line.startswith("+++ "):
            # "+++ b/path/to/file" (or "+++ /dev/null" for a pure deletion,
            # which correctly yields no entry -- nothing to scope a
            # CRITICAL/HIGH finding against in a file that no longer exists).
            path = line[4:]
            current_file = path[2:] if path.startswith("b/") else (
                None if path == "/dev/null" else path
            )
            if current_file is not None:
                ranges.setdefault(current_file, set())
            continue
        if line.startswith("@@") and current_file is not None:
            m = _HUNK_HEADER_RE.match(line)
            if m:
                start = int(m.group(1))
                count = int(m.group(2)) if m.group(2) is not None else 1
                if count > 0:
                    ranges[current_file].update(range(start, start + count))
    return ranges


def read_file_contents(files: List[str], project_root: str = ".") -> dict[str, str]:
    """Read contents of a list of files, resolved relative to project_root.
    Skip unreadable ones."""
    contents = {}
    for f in files:
        try:
            contents[f] = (Path(project_root) / f).read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            print(f"[agent] Could not read {f}: {e}", file=sys.stderr)
    return contents


def find_existing_tests(changed_files: List[str]) -> dict[str, str]:
    """Try to find existing test files for the changed source files."""
    test_contents = {}
    for src in changed_files:
        p = Path(src)
        candidates = [
            p.parent / f"test_{p.name}",
            p.parent / f"tests/test_{p.name}",
            Path("tests") / f"test_{p.name}",
            Path("test") / f"test_{p.name}",
        ]
        for candidate in candidates:
            if candidate.exists():
                try:
                    test_contents[str(candidate)] = candidate.read_text(encoding="utf-8")
                except Exception:
                    pass
                break
    return test_contents


# ──────────────────────────────────────────────
# Main run function
# ──────────────────────────────────────────────

def _tests_output_path(changed_files: List[str]) -> Path:
    tests_dir = Path("tests")
    if len(changed_files) == 1:
        stem = Path(changed_files[0]).stem
        return tests_dir / f"test_{stem}.py"
    return tests_dir / "test_changes.py"


def write_and_commit_tests(generated_tests: str, changed_files: List[str]) -> None:
    """Write generated tests to tests/ and create a git commit."""
    if not generated_tests or not generated_tests.strip():
        print("[agent] No generated tests to commit.")
        return

    out_path = _tests_output_path(changed_files)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(generated_tests.strip() + "\n", encoding="utf-8")
    print(f"[agent] Written generated tests to: {out_path}")

    files_str = ", ".join(Path(f).name for f in changed_files)
    try:
        subprocess.run(["git", "add", str(out_path)], check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", f"test: add AI-generated tests for {files_str}"],
            check=True, capture_output=True,
        )
        print(f"[agent] Committed {out_path}")
    except subprocess.CalledProcessError as e:
        print(f"[agent] git commit failed: {e.stderr.strip()}", file=sys.stderr)


def run(
    mode: str = "local",
    base_ref: str = "origin/main",
    pr_number: Optional[str] = None,
    project_root: str = ".",
    generate_test_cases: bool = True,
    commit_tests: bool = False,
) -> int:
    """
    Run the full QA pipeline.

    Args:
        mode:             "local" (terminal output) or "ci" (GitHub Actions)
        base_ref:         Git ref to diff against (default: origin/main)
        pr_number:        PR number (CI mode only)
        project_root:     Root directory of the project being reviewed
        generate_test_cases: Whether to ask the AI to generate tests

    Returns:
        0 if all checks pass, 1 if blocking issues found, 2 on fatal error.
    """

    # ── 0. Load per-repo QA config (.timefrugal-qa.yml), if any ───────
    repo_config = load_repo_config(project_root)

    # ── 1. Discover changed files ──────────────────────────────────────
    print("[agent] Discovering changed files...")
    changed = get_changed_files(base_ref, project_root=project_root)

    if not changed:
        print("[agent] No supported files changed (Python, Java, HTML). Nothing to review.")
        if mode == "ci":
            _set_ci_status(blocked=False)
        return 0

    language = detect_language(changed)
    print(f"[agent] Language detected: {language} | Files to review: {', '.join(changed)}")

    # ── 2. Read file contents + real diff boundary ─────────────────────
    file_contents = read_file_contents(changed, project_root=project_root)
    existing_tests = find_existing_tests(changed) if language == "python" else {}
    diff_text = get_diff_text(base_ref, changed, project_root=project_root)
    changed_line_ranges = get_changed_line_ranges(base_ref, changed, project_root=project_root)

    # ── 3. Static analysis ────────────────────────────────────────────
    print(f"[agent] Running static analysis for {language}...")
    static_results = run_all(changed, project_root=project_root, repo_config=repo_config)
    s = static_results.summary()
    print(
        f"[agent] Static analysis complete — "
        f"CRITICAL:{s['CRITICAL']} HIGH:{s['HIGH']} MEDIUM:{s['MEDIUM']} LOW:{s['LOW']}"
    )

    # ── 4+5. AI code review + test generation (parallel) ──────────────
    skip_tests = not generate_test_cases or not file_contents or language == "html"
    print("[agent] Sending to AI provider chain for code review"
          + (" + test generation..." if not skip_tests else "..."))
    with ThreadPoolExecutor(max_workers=2) as pool:
        review_future = pool.submit(
            review_code,
            file_contents=file_contents,
            static_results=static_results,
            repo_name=config.GITHUB_REPOSITORY,
            language=language,
            repo_config=repo_config,
            diff_text=diff_text,
            changed_line_ranges=changed_line_ranges,
        )
        tests_future = (
            pool.submit(generate_tests, file_contents, existing_tests, language)
            if not skip_tests else None
        )
        ai_review = review_future.result()
        generated_tests = tests_future.result() if tests_future is not None else ""

    if ai_review.errors:
        for err in ai_review.errors:
            print(f"[agent] AI review warning: {err}", file=sys.stderr)

    if commit_tests and mode == "local" and generated_tests:
        write_and_commit_tests(generated_tests, changed)

    # ── 6. Determine overall verdict ──────────────────────────────────
    blocked = static_results.has_blocking_issues or ai_review.has_blocking_issues
    errored = mode == "ci" and bool(static_results.errors or ai_review.errors)

    # ── 7. Report ─────────────────────────────────────────────────────
    if mode == "ci":
        _report_ci(
            pr_number=pr_number or config.PR_NUMBER,
            static_results=static_results,
            ai_review=ai_review,
            generated_tests=generated_tests,
            blocked=blocked,
            errored=errored,
        )
    else:
        _report_local(
            static_results=static_results,
            ai_review=ai_review,
            generated_tests=generated_tests,
            changed_files=changed,
        )

    return 1 if blocked else (2 if errored else 0)


def _report_ci(pr_number, static_results, ai_review, generated_tests, blocked, errored):
    """Report to GitHub PR comment, commit status, and Actions step summary."""
    from qa_agent.pr_reporter import post_pr_comment, set_commit_status, write_step_summary
    if pr_number:
        print("[agent] Posting PR comment...")
        post_pr_comment(pr_number, static_results, ai_review, generated_tests)
    print("[agent] Setting commit status check...")
    set_commit_status(blocked=blocked, errored=errored)
    write_step_summary(static_results, ai_review, generated_tests)


def _report_local(static_results, ai_review, generated_tests, changed_files):
    """Report to terminal and save markdown report."""
    from qa_agent.local_reporter import print_report, save_report
    print_report(static_results, ai_review, generated_tests, changed_files)
    path = save_report(static_results, ai_review, generated_tests)
    print(f"\n[agent] Report saved to: {path}")


def _set_ci_status(blocked: bool):
    """Set CI status without posting a comment."""
    from qa_agent.pr_reporter import set_commit_status
    set_commit_status(blocked=blocked)
