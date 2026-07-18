"""
Timefrugal-QA Agent — main orchestrator.
Determines changed files, runs all analysis, generates tests, then reports.
"""
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import List, Optional

from qa_agent import config
from qa_agent.static_analysis import run_all, detect_language, AnalysisResults
from qa_agent.ai_review import review_code, generate_tests


# ──────────────────────────────────────────────
# Git utilities
# ──────────────────────────────────────────────

def get_changed_files(base_ref: str = "origin/main") -> List[str]:
    """Return list of changed Python, Java, and HTML files compared to base_ref."""
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", "--diff-filter=ACMR", base_ref, "HEAD"],
            capture_output=True, text=True, check=True,
        )
        files = [
            f.strip() for f in result.stdout.splitlines()
            if Path(f.strip()).suffix.lower() in config.SUPPORTED_EXTENSIONS
            and Path(f.strip()).exists()
            and not any(excl in f for excl in config.EXCLUDE_PATTERNS)
        ]
        return files
    except subprocess.CalledProcessError as e:
        print(f"[agent] git diff failed: {e.stderr.strip()}", file=sys.stderr)
        return []


def read_file_contents(files: List[str]) -> dict[str, str]:
    """Read contents of a list of files. Skip unreadable ones."""
    contents = {}
    for f in files:
        try:
            contents[f] = Path(f).read_text(encoding="utf-8", errors="replace")
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

    # ── 1. Discover changed files ──────────────────────────────────────
    print("[agent] Discovering changed files...")
    changed = get_changed_files(base_ref)

    if not changed:
        print("[agent] No supported files changed (Python, Java, HTML). Nothing to review.")
        if mode == "ci":
            _set_ci_status(blocked=False)
        return 0

    language = detect_language(changed)
    print(f"[agent] Language detected: {language} | Files to review: {', '.join(changed)}")

    # ── 2. Read file contents ──────────────────────────────────────────
    file_contents = read_file_contents(changed)
    existing_tests = find_existing_tests(changed) if language == "python" else {}

    # ── 3. Static analysis ────────────────────────────────────────────
    print(f"[agent] Running static analysis for {language}...")
    static_results = run_all(changed, project_root=project_root)
    s = static_results.summary()
    print(
        f"[agent] Static analysis complete — "
        f"CRITICAL:{s['CRITICAL']} HIGH:{s['HIGH']} MEDIUM:{s['MEDIUM']} LOW:{s['LOW']}"
    )

    # ── 4+5. AI code review + test generation (parallel) ──────────────
    skip_tests = not generate_test_cases or not file_contents or language == "html"
    print(f"[agent] Sending to GitHub Models AI ({config.AI_MODEL}) for code review"
          + (" + test generation..." if not skip_tests else "..."))
    with ThreadPoolExecutor(max_workers=2) as pool:
        review_future = pool.submit(
            review_code,
            file_contents=file_contents,
            static_results=static_results,
            repo_name=config.GITHUB_REPOSITORY,
            language=language,
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

    return 2 if errored else (1 if blocked else 0)


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
