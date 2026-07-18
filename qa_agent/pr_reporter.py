"""
Posts structured review results as GitHub PR comments and sets commit status checks.
"""
import json
import os
import time
from typing import Callable, Optional

import requests

from qa_agent import config
from qa_agent.ai_review import AIReview
from qa_agent.static_analysis import AnalysisResults


SEVERITY_EMOJI = {
    "CRITICAL": "🔴",
    "HIGH": "🟠",
    "MEDIUM": "🟡",
    "LOW": "🔵",
    "INFO": "⚪",
}

CHECK_NAME = "Timefrugal-QA"


def _request_with_retry(method: Callable, url: str, **kwargs) -> requests.Response:
    """Make an HTTP request, retrying up to 3 times on HTTP 429 with exponential backoff."""
    for attempt in range(3):
        resp = method(url, **kwargs)
        if resp.status_code != 429 or attempt == 2:
            return resp
        time.sleep(5.0 * (2 ** attempt))
    return resp  # satisfies type checker


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {config.GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _api(path: str) -> str:
    return f"{config.GITHUB_API_URL}{path}"


# ──────────────────────────────────────────────
# PR comment
# ──────────────────────────────────────────────

def post_pr_comment(
    pr_number: str,
    static_results: AnalysisResults,
    ai_review: AIReview,
    generated_tests: str = "",
) -> bool:
    """
    Post (or update) the QA review comment on the given PR.
    Returns True on success.
    """
    repo = config.GITHUB_REPOSITORY
    if not repo or not pr_number:
        print("[pr_reporter] Skipping comment: GITHUB_REPOSITORY or PR_NUMBER not set.")
        return False

    body = _build_comment(static_results, ai_review, generated_tests)

    # Check for existing comment to update (avoid comment spam on re-runs)
    existing_id = _find_existing_comment(repo, pr_number)
    if existing_id:
        url = _api(f"/repos/{repo}/issues/comments/{existing_id}")
        resp = _request_with_retry(requests.patch, url, headers=_headers(), json={"body": body}, timeout=30)
    else:
        url = _api(f"/repos/{repo}/issues/{pr_number}/comments")
        resp = _request_with_retry(requests.post, url, headers=_headers(), json={"body": body}, timeout=30)

    if resp.status_code not in (200, 201):
        print(f"[pr_reporter] Failed to post comment: {resp.status_code} {resp.text[:200]}")
        return False
    return True


def set_commit_status(blocked: bool, errored: bool = False, description: str = "") -> bool:
    """
    Set a GitHub commit status check (success/failure/error).
    Returns True on success.
    """
    repo = config.GITHUB_REPOSITORY
    sha = config.GITHUB_SHA
    if not repo or not sha:
        print("[pr_reporter] Skipping status check: GITHUB_REPOSITORY or GITHUB_SHA not set.")
        return False

    state = "failure" if blocked else "error" if errored else "success"
    desc = description or (
        (
            "QA failed — blocking issues found (some tools also failed). Review the PR comment."
            if errored
            else "QA failed — blocking issues found. Review the PR comment."
        )
        if blocked
        else "QA could not fully run — tool failures. Review the PR comment."
        if errored
        else "All QA checks passed ✅"
    )

    url = _api(f"/repos/{repo}/statuses/{sha}")
    payload = {
        "state": state,
        "description": desc[:139],   # GitHub limit
        "context": CHECK_NAME,
        "target_url": f"https://github.com/{repo}/pull/{config.PR_NUMBER}",
    }
    resp = _request_with_retry(requests.post, url, headers=_headers(), json=payload, timeout=30)
    return resp.status_code == 201


# ──────────────────────────────────────────────
# GitHub Actions step summary
# ──────────────────────────────────────────────

def write_step_summary(
    static_results: AnalysisResults,
    ai_review: AIReview,
    generated_tests: str = "",
) -> None:
    """Append the QA report to $GITHUB_STEP_SUMMARY if running in GitHub Actions."""
    summary_file = os.getenv("GITHUB_STEP_SUMMARY")
    if not summary_file:
        return
    body = _build_comment(static_results, ai_review, generated_tests)
    body = body.replace("<!-- timefrugal-qa-comment -->\n", "")
    try:
        with open(summary_file, "a", encoding="utf-8") as f:
            f.write(body + "\n")
    except Exception as e:
        print(f"[pr_reporter] Could not write step summary: {e}")


# ──────────────────────────────────────────────
# Comment body builder
# ──────────────────────────────────────────────

def _build_comment(
    static: AnalysisResults,
    ai: AIReview,
    generated_tests: str,
) -> str:
    blocked = static.has_blocking_issues or ai.has_blocking_issues
    errored = bool(static.errors or ai.errors)
    status_line = (
        "## 🔴 Timefrugal-QA — BLOCKED: Critical/High issues require attention before merge"
        + (
            " (Note: some analysis tools also failed to complete — see Tool Warnings below.)"
            if errored
            else ""
        )
        if blocked
        else "## ⚠️ Timefrugal-QA — ERRORED: analysis tools failed, results incomplete"
        if errored
        else "## ✅ Timefrugal-QA — All checks passed"
    )

    parts = [
        "<!-- timefrugal-qa-comment -->",
        status_line,
        "",
    ]

    # AI summary
    if ai.summary:
        parts += ["### 📋 Summary", ai.summary, ""]

    # Static analysis summary table
    s = static.summary()
    parts += [
        "### 🔍 Static Analysis",
        "| Severity | Count |",
        "|----------|-------|",
        f"| 🔴 Critical | {s['CRITICAL']} |",
        f"| 🟠 High     | {s['HIGH']} |",
        f"| 🟡 Medium   | {s['MEDIUM']} |",
        f"| 🔵 Low      | {s['LOW']} |",
        f"| ⚪ Info      | {s['INFO']} |",
        "",
    ]

    # Static findings (collapsible)
    if static.findings:
        critical_high = [
            f for f in static.findings
            if f.severity in ("CRITICAL", "HIGH")
        ]
        lower = [
            f for f in static.findings
            if f.severity not in ("CRITICAL", "HIGH")
        ]

        if critical_high:
            parts.append("#### ⚠️ Critical & High Issues")
            for f in critical_high:
                emoji = SEVERITY_EMOJI.get(f.severity, "⚪")
                parts.append(
                    f"- {emoji} **[{f.severity}]** `{f.file}:{f.line}` — "
                    f"**{f.tool}** {f.message}"
                )
            parts.append("")

        if lower:
            parts.append("<details>")
            parts.append(f"<summary>📄 Medium / Low / Info findings ({len(lower)})</summary>")
            parts.append("")
            for f in lower:
                emoji = SEVERITY_EMOJI.get(f.severity, "⚪")
                parts.append(
                    f"- {emoji} **[{f.severity}]** `{f.file}:{f.line}` — "
                    f"**{f.tool}** {f.message}"
                )
            parts.append("")
            parts.append("</details>")
            parts.append("")

    # AI review findings
    if ai.findings:
        parts.append("### 🤖 AI Code Review")
        for f in sorted(ai.findings, key=lambda x: config.SEVERITY_ORDER.index(x.severity)):
            emoji = SEVERITY_EMOJI.get(f.severity, "⚪")
            loc = f"`{f.file}:{f.line}`" if f.line else f"`{f.file}`"
            parts.append(f"- {emoji} **[{f.severity} / {f.category}]** {loc}")
            parts.append(f"  - **Issue:** {f.message}")
            if f.suggestion:
                parts.append(f"  - **Fix:** {f.suggestion}")
        parts.append("")

    # Architecture notes
    if ai.architecture_notes:
        parts += [
            "<details>",
            "<summary>🏗️ Architecture & Design Notes</summary>",
            "",
            ai.architecture_notes,
            "",
            "</details>",
            "",
        ]

    # Generated tests
    if generated_tests and generated_tests.strip():
        parts += [
            "<details>",
            "<summary>🧪 AI-Generated Test Cases</summary>",
            "",
            "```python",
            generated_tests.strip(),
            "```",
            "",
            "</details>",
            "",
        ]

    # Tool errors (if any)
    if static.errors or ai.errors:
        all_errors = static.errors + ai.errors
        parts += [
            "<details>",
            "<summary>⚙️ Tool Warnings</summary>",
            "",
        ]
        for e in all_errors:
            parts.append(f"- {e}")
        parts += ["", "</details>", ""]

    parts.append(
        "_Powered by [Timefrugal-QA](https://github.com/Timefrugal/Timefrugal-QA) "
        "· Free AI via GitHub Models · Open-source analysis tools_"
    )

    return "\n".join(parts)


# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────

def _find_existing_comment(repo: str, pr_number: str) -> Optional[int]:
    """Find an existing Timefrugal-QA comment on the PR to update instead of posting a new one."""
    url = _api(f"/repos/{repo}/issues/{pr_number}/comments")
    params = {"per_page": 100}
    try:
        resp = _request_with_retry(requests.get, url, headers=_headers(), params=params, timeout=30)
        if resp.status_code != 200:
            return None
        for comment in resp.json():
            if "<!-- timefrugal-qa-comment -->" in comment.get("body", ""):
                return comment["id"]
    except Exception:
        pass
    return None
