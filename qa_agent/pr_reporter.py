"""
Posts structured review results as GitHub PR comments and sets commit status checks.
"""
import json
import os
import sys
import time
from typing import Callable, Optional

import requests

from qa_agent import config
from qa_agent.ai_review import AIReview
from qa_agent.static_analysis import (
    AnalysisResults,
    blocking_severity_label,
    effective_block_threshold,
)


SEVERITY_EMOJI = {
    "CRITICAL": "🔴",
    "HIGH": "🟠",
    "MEDIUM": "🟡",
    "LOW": "🔵",
    "INFO": "⚪",
}

CHECK_NAME = "Timefrugal-QA"

# GitHub's own hard limit on an issue/PR comment body (IssueComment.body is
# validated server-side at exactly this size) -- a comment over this fails
# outright with HTTP 422, which previously meant a large-diff PR's real QA
# result (pass or fail) silently never got posted at all. Confirmed live
# 2026-09-07: 598 medium + 356 low + 1143 info findings on one file alone
# built a body well over this limit.
_GITHUB_COMMENT_MAX_CHARS = 65536

# Inline cap on the non-critical/high findings list specifically -- this is
# the actual usual source of an oversized body (hundreds of near-duplicate
# bandit/semgrep findings, e.g. the same "partial executable path" warning
# repeated per subprocess call site), not the AI review or generated tests,
# which are the more valuable, PR-specific sections and come later in the
# comment. Capping this list is what actually prevents oversized bodies in
# the common case; the whole-body truncation below is a last-resort safety
# net for whatever this cap doesn't cover.
_MAX_LOWER_FINDINGS_SHOWN = 150


def _truncate_to_github_limit(body: str) -> str:
    """Last-resort safety net: if the assembled comment is still over
    GitHub's hard size limit after the findings-list cap above (e.g. a
    very large AI review or generated-test block), truncate rather than
    let post_pr_comment fail outright with a 422 and post nothing at all.
    A truncated-but-posted comment (with a real status still set via
    set_commit_status, which never depends on comment length) is strictly
    more useful than a real QA result vanishing silently."""
    if len(body) <= _GITHUB_COMMENT_MAX_CHARS:
        return body
    notice = (
        "\n\n---\n_⚠️ Report truncated -- the full result exceeded GitHub's "
        "comment size limit. See the `qa-report` workflow artifact "
        "(qa_report.md) for the complete output._\n"
    )
    return body[: _GITHUB_COMMENT_MAX_CHARS - len(notice)] + notice


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
    if resp.status_code == 201:
        print(f"[pr_reporter] Commit status '{state}' set on {sha}")
        return True
    print(f"[pr_reporter] Failed to set commit status on {sha}: {resp.status_code} {resp.text[:200]}", file=sys.stderr)
    return False


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
    threshold = effective_block_threshold(static, ai.has_blocking_issues)
    status_line = (
        f"## 🔴 Timefrugal-QA — BLOCKED: {blocking_severity_label(threshold)} "
        "issues require attention before merge"
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
    note = _threshold_source_note(static, threshold)
    if note:
        parts += [note, ""]

    # Which provider actually served the AI review -- added 2026-09-07 so
    # a report never leaves this unknowable after the fact. Only shown
    # once a provider actually succeeded (ai.provider set by review_code
    # from _call_with_fallback's return); silent when AI review wasn't
    # configured or every provider failed (ai.errors already covers that
    # case via the Tool Warnings section below).
    if ai.provider:
        parts += [f"_AI review served by **{ai.provider}** (`{ai.provider_model}`)_", ""]

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
            shown = lower[:_MAX_LOWER_FINDINGS_SHOWN]
            for f in shown:
                emoji = SEVERITY_EMOJI.get(f.severity, "⚪")
                parts.append(
                    f"- {emoji} **[{f.severity}]** `{f.file}:{f.line}` — "
                    f"**{f.tool}** {f.message}"
                )
            omitted = len(lower) - len(shown)
            if omitted > 0:
                parts.append(
                    f"- _...and {omitted} more not shown here (see the "
                    "`qa-report` workflow artifact / qa_report.md for the "
                    "complete list)._"
                )
            parts.append("")
            parts.append("</details>")
            parts.append("")

    # AI review findings
    if ai.findings:
        parts.append("### 🤖 AI Code Review")
        for af in sorted(ai.findings, key=lambda x: config.SEVERITY_ORDER.index(x.severity)):
            emoji = SEVERITY_EMOJI.get(af.severity, "⚪")
            loc = f"`{af.file}:{af.line}`" if af.line else f"`{af.file}`"
            parts.append(f"- {emoji} **[{af.severity} / {af.category}]** {loc}")
            parts.append(f"  - **Issue:** {af.message}")
            if af.suggestion:
                parts.append(f"  - **Fix:** {af.suggestion}")
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

    # Was a hardcoded "Free AI via Groq" regardless of which provider (or
    # whether any at all) actually served the review -- misleading once a
    # repo configures Cerebras/Mistral/QA_FALLBACK_MODEL fallback, since
    # any of those could be the one that actually answered. Now reflects
    # ai.provider when a review succeeded; falls back to the original
    # generic wording when it didn't (no AI review configured, or every
    # provider failed -- ai.errors already covers why).
    ai_credit = f"AI review via {ai.provider}" if ai.provider else "Free AI"
    parts.append(
        "_Powered by [Timefrugal-QA](https://github.com/Timefrugal/Timefrugal-QA) "
        f"· {ai_credit} · Open-source analysis tools_"
    )

    return _truncate_to_github_limit("\n".join(parts))


# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────

def _threshold_source_note(static: AnalysisResults, threshold: Optional[str]) -> str:
    """One-line explanation of a stricter-than-default blocking cutoff, or ""
    when there's nothing surprising to explain (not blocked, or blocked at the
    CRITICAL/HIGH cutoff the header wording already conveys).

    Without this, a reader of a stricter repo's report sees "BLOCKED:
    Medium-or-above" above a table of 0 Critical / 0 High and has no way to
    tell a deliberate per-repo config from a broken gate -- which is exactly
    how jarvis-infra#323 came to be filed against a correctly-working gate.
    """
    if not threshold or threshold in (config.SEVERITY_CRITICAL, config.SEVERITY_HIGH):
        return ""
    source = (
        "`block_merge_threshold` in this repo's `.timefrugal-qa.yml`"
        if static.block_merge_threshold
        else "the `QA_BLOCK_MERGE_THRESHOLD` environment variable"
    )
    return (
        f"_This repo gates merges at **{threshold}** severity and above (set by "
        f"{source}) — stricter than the {config.SEVERITY_HIGH} default, so the "
        f"findings below block even with no Critical/High present._"
    )


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
