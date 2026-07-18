"""
AI-powered code review using GitHub Models (free).
Uses the OpenAI-compatible endpoint at models.inference.ai.azure.com
with the user's GITHUB_TOKEN — no extra billing.
"""
import json
import os
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional, TypeVar

import openai
from openai import OpenAI

from qa_agent import config
from qa_agent.repo_config import RepoConfig
from qa_agent.static_analysis import AnalysisResults

# The only severities the AI's JSON response is allowed to carry. Anything
# else (hallucinated string, wrong case, missing field) is treated as the
# lowest, non-blocking severity rather than trusted outright (H1: AI findings
# shouldn't independently block with unvalidated severity).
_VALID_SEVERITIES = set(config.SEVERITY_ORDER)


def _validate_severity(raw) -> str:
    sev = str(raw).strip().upper() if raw else ""
    return sev if sev in _VALID_SEVERITIES else config.SEVERITY_INFO


@dataclass
class AIFinding:
    severity: str
    category: str       # "bug" | "security" | "architecture" | "design" | "performance" | "test"
    file: str
    line: int
    message: str
    suggestion: str


@dataclass
class AIReview:
    summary: str = ""
    findings: List[AIFinding] = field(default_factory=list)
    generated_tests: str = ""      # pytest code block
    architecture_notes: str = ""
    errors: List[str] = field(default_factory=list)
    # AI findings are advisory-only (never block a merge on their own) unless
    # the target repo's .timefrugal-qa.yml explicitly opts in via `ai.blocking:
    # true` (H1: AI findings shouldn't independently block with unvalidated
    # severity). Set by review_code() from the repo_config it was given.
    ai_blocking: bool = False

    @property
    def has_blocking_issues(self) -> bool:
        if not self.ai_blocking:
            return False
        return any(
            f.severity in (config.SEVERITY_CRITICAL, config.SEVERITY_HIGH)
            for f in self.findings
        )


# ──────────────────────────────────────────────
# Prompts
# ──────────────────────────────────────────────

_REVIEW_JSON_SCHEMA = """\
Respond ONLY in valid JSON matching this schema:
{
  "summary": "<2-3 sentence overall assessment>",
  "architecture_notes": "<paragraph on design/architecture observations, empty string if none>",
  "findings": [
    {
      "severity": "CRITICAL|HIGH|MEDIUM|LOW|INFO",
      "category": "bug|security|architecture|design|performance|quality",
      "file": "<filename>",
      "line": <int or 0 if unknown>,
      "message": "<what is wrong>",
      "suggestion": "<concrete fix or improvement>"
    }
  ]
}
Be precise and actionable. Do not hallucinate line numbers — use 0 if uncertain."""

_SYSTEM_PROMPTS: dict[str, str] = {
    "python": f"""You are a senior software engineer with 15+ years of experience reviewing Python code.
Your role:
1. Identify bugs, logic errors, and edge cases
2. Spot security vulnerabilities (injection, auth bypass, insecure defaults, secrets in code, etc.)
3. Evaluate architecture and design — suggest improvements where patterns are wrong or fragile
4. Flag performance bottlenecks
5. Note missing or inadequate error handling
6. Assess testability

{_REVIEW_JSON_SCHEMA}
""",
    "java": f"""You are a senior software engineer with 15+ years of experience reviewing Java code.
Your role:
1. Identify bugs, logic errors, null pointer risks, and edge cases
2. Spot security vulnerabilities (SQL injection, deserialization, XXE, SSRF, hardcoded secrets, etc.)
3. Evaluate architecture and design — flag anti-patterns, poor use of generics, or tight coupling
4. Flag performance issues (inefficient collections, N+1 queries, synchronization problems)
5. Note missing or inadequate exception handling and resource leaks
6. Assess testability and dependency injection

{_REVIEW_JSON_SCHEMA}
""",
    "html": f"""You are a senior frontend developer with 15+ years of experience reviewing HTML templates.
Your role:
1. Identify XSS vectors — unescaped output, dangerous attribute values, inline event handlers
2. Flag accessibility issues — missing alt text, poor semantic structure, unlabelled form fields
3. Spot broken or unsafe links, missing CSP meta tags, and insecure form actions (HTTP action on HTTPS page)
4. Note deprecated or non-standard elements and attributes
5. Flag missing viewport meta, charset declarations, or ARIA misuse
6. Assess overall semantic correctness and SEO impact

{_REVIEW_JSON_SCHEMA}
""",
}

_TEST_SYSTEM_PROMPTS: dict[str, str] = {
    "python": """You are a senior Python test engineer with 15+ years of experience.
Generate comprehensive pytest test cases for the provided Python code.

Requirements:
- Use pytest and standard library only (no extra test deps unless absolutely necessary)
- Cover: happy paths, edge cases, error/exception paths, boundary conditions
- Include mocks where external dependencies exist (use unittest.mock)
- Each test must have a clear docstring explaining what it tests
- Tests must be runnable as-is (correct imports, no placeholders)

Respond with ONLY the raw Python test code, no markdown fences, no explanation.
Start with the import block.
""",
    "java": """You are a senior Java test engineer with 15+ years of experience.
Generate comprehensive JUnit 5 test cases for the provided Java code.

Requirements:
- Use JUnit 5 (@Test, @BeforeEach, Assertions) and Mockito for mocking
- Cover: happy paths, edge cases, exceptions, null inputs, boundary conditions
- Use descriptive @DisplayName annotations explaining what each test verifies
- Tests must compile and run with standard JUnit 5 + Mockito imports

Respond with ONLY the raw Java test code, no markdown fences, no explanation.
Start with the package declaration if present, then imports.
""",
}


def _get_review_prompt(language: str) -> str:
    return _SYSTEM_PROMPTS.get(language, _SYSTEM_PROMPTS["python"])


def _get_test_prompt(language: str) -> str:
    return _TEST_SYSTEM_PROMPTS.get(language, _TEST_SYSTEM_PROMPTS["python"])


_LANG_FENCE: dict[str, str] = {"python": "python", "java": "java", "html": "html"}


# ──────────────────────────────────────────────
# Retry helper
# ──────────────────────────────────────────────

_T = TypeVar("_T")


def _call_with_retry(fn: Callable[[], _T]) -> _T:
    """Call fn(), retrying on HTTP 429 (rate limit) with exponential backoff."""
    for attempt in range(config.AI_RETRY_MAX_ATTEMPTS):
        try:
            return fn()
        except openai.RateLimitError:
            if attempt == config.AI_RETRY_MAX_ATTEMPTS - 1:
                raise
            delay = config.AI_RETRY_BASE_DELAY * (2 ** attempt)
            time.sleep(delay)
    raise RuntimeError("unreachable")  # satisfies type checker


# ──────────────────────────────────────────────
# Client factory
# ──────────────────────────────────────────────

def _get_client() -> OpenAI:
    token = config.GITHUB_TOKEN
    if not token:
        raise ValueError(
            "GITHUB_TOKEN environment variable not set. "
            "Required to access GitHub Models free AI."
        )
    return OpenAI(
        base_url=config.GITHUB_MODELS_BASE_URL,
        api_key=token,
    )


# ──────────────────────────────────────────────
# Review functions
# ──────────────────────────────────────────────

def review_code(
    file_contents: dict[str, str],
    static_results: AnalysisResults,
    repo_name: str = "",
    language: str = "python",
    repo_config: Optional[RepoConfig] = None,
) -> AIReview:
    """
    Send changed file contents + static analysis findings to GitHub Models AI.
    Returns structured AIReview.
    """
    review = AIReview(ai_blocking=bool(repo_config.ai_blocking) if repo_config else False)

    if not file_contents:
        review.errors.append("No file contents provided for AI review.")
        return review

    try:
        client = _get_client()
    except ValueError as e:
        review.errors.append(str(e))
        return review

    # Build the user message
    fence = _LANG_FENCE.get(language, language)
    code_sections = []
    for filepath, content in file_contents.items():
        truncated = content[:6000] + ("\n... [truncated]" if len(content) > 6000 else "")
        code_sections.append(f"### File: {filepath}\n```{fence}\n{truncated}\n```")

    static_summary = _format_static_for_ai(static_results)

    user_msg = f"""Repository: {repo_name or "unknown"}

## Changed Files
{chr(10).join(code_sections)}

## Static Analysis Pre-scan Results
{static_summary}

Please perform a thorough code review of the changed files above.
"""

    try:
        response = _call_with_retry(lambda: client.chat.completions.create(
            model=config.AI_MODEL,
            messages=[
                {"role": "system", "content": _get_review_prompt(language)},
                {"role": "user", "content": user_msg},
            ],
            max_tokens=config.AI_MAX_TOKENS,
            temperature=0.1,
        ))
        raw = response.choices[0].message.content or "{}"
        # Strip markdown fences if model wraps JSON
        raw = raw.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        review.errors.append(f"AI response was not valid JSON: {e}")
        return review
    except Exception as e:
        review.errors.append(f"GitHub Models API error: {e}")
        return review

    review.summary = data.get("summary", "")
    review.architecture_notes = data.get("architecture_notes", "")

    for item in data.get("findings", []):
        review.findings.append(AIFinding(
            severity=_validate_severity(item.get("severity", config.SEVERITY_INFO)),
            category=item.get("category", "quality"),
            file=item.get("file", ""),
            line=item.get("line", 0),
            message=item.get("message", ""),
            suggestion=item.get("suggestion", ""),
        ))

    return review


def generate_tests(
    file_contents: dict[str, str],
    existing_test_files: Optional[dict[str, str]] = None,
    language: str = "python",
) -> str:
    """
    Generate test cases for the provided source files.
    Returns Python (pytest) or Java (JUnit 5) test code; empty string for HTML.
    """
    if not file_contents or language == "html":
        return ""

    try:
        client = _get_client()
    except ValueError as e:
        return f"# Error: {e}\n"

    fence = _LANG_FENCE.get(language, language)
    code_sections = []
    for filepath, content in file_contents.items():
        truncated = content[:5000] + ("\n... [truncated]" if len(content) > 5000 else "")
        code_sections.append(f"### Source: {filepath}\n```{fence}\n{truncated}\n```")

    existing_sections = []
    if existing_test_files:
        for filepath, content in existing_test_files.items():
            truncated = content[:2000] + ("\n... [truncated]" if len(content) > 2000 else "")
            existing_sections.append(
                f"### Existing tests: {filepath}\n```python\n{truncated}\n```"
            )

    user_msg = f"""## Source Code to Test
{chr(10).join(code_sections)}
"""
    if existing_sections:
        user_msg += f"\n## Existing Tests (do not duplicate these)\n{chr(10).join(existing_sections)}\n"

    user_msg += "\nGenerate new comprehensive pytest test cases for the source code above."

    try:
        response = _call_with_retry(lambda: client.chat.completions.create(
            model=config.AI_MODEL,
            messages=[
                {"role": "system", "content": _get_test_prompt(language)},
                {"role": "user", "content": user_msg},
            ],
            max_tokens=config.AI_MAX_TOKENS,
            temperature=0.1,
        ))
        test_code = response.choices[0].message.content or ""
        # Strip markdown fences if present
        test_code = test_code.strip()
        if test_code.startswith("```"):
            parts = test_code.split("```")
            test_code = parts[1]
            if test_code.startswith("python"):
                test_code = test_code[6:]
        return test_code.strip()
    except Exception as e:
        return f"# Error generating tests: {e}\n"


# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────

def _format_static_for_ai(results: AnalysisResults) -> str:
    if not results.findings:
        return "No issues found by static analysis tools."

    lines = []
    summary = results.summary()
    lines.append(
        f"Findings: CRITICAL={summary['CRITICAL']}, HIGH={summary['HIGH']}, "
        f"MEDIUM={summary['MEDIUM']}, LOW={summary['LOW']}"
    )
    # Show top findings only (cap at 20 to stay within token budget)
    top = sorted(
        results.findings,
        key=lambda f: ["CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO"].index(f.severity)
    )[:20]
    for f in top:
        lines.append(f"- [{f.severity}] {f.tool} | {f.file}:{f.line} | {f.message}")
    return "\n".join(lines)
