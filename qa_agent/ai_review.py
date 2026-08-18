"""
AI-powered code review using a chain of free-tier providers (Groq, then
Cerebras, then Mistral, then an optional env-gated last-resort fallback --
see config.AI_PROVIDERS for the authoritative order) — no extra billing on
any of them. Providers are tried in order; a provider that's out of quota,
down, returns unparseable content, or simply not configured (missing API
key) is skipped in favor of the next one, so a single provider running out
(or misbehaving) doesn't take the whole AI review down. (GitHub Models,
the original backend, was fully retired by GitHub on 2026-07-30.)
"""
import json
import sys
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional, TypeVar

import openai
from openai import OpenAI
from openai.types.shared_params import ResponseFormatJSONSchema

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

# Decoding-time enforcement of _REVIEW_JSON_SCHEMA above, for providers that
# can't be trusted to follow it from plain-text instructions alone. Scoped to
# QA_FALLBACK_MODEL only (see the identical think:false scoping in
# review_code's _make_request) -- Groq/Cerebras/Mistral already return
# schema-compliant JSON from the plain prompt in production, so this is
# deliberately not applied to them: an unrecognized/differently-behaving
# response_format could change their behavior for no benefit.
#
# Also gated on config.QA_FALLBACK_RESPONSE_FORMAT (default on) -- see that
# flag's docstring in config.py. Not every model a consumer repo points
# QA_FALLBACK_MODEL at needs this, and it isn't free when unneeded: a real
# eval found a model with an already-reliable unconstrained JSON track
# record start failing on truncation once this was force-applied to it.
#
# Why this exists: independent testing (several local/self-hosted 7-8B
# models against a real diff-review prompt) found models that write good,
# relevant review content but never emit valid JSON on instructions alone --
# not a capability gap, a formatting one. `response_format` forces
# syntactically valid JSON at decode time regardless of instruction-following.
#
# DO NOT weaken this to plain `{"type": "json_object"}` mode as a "simpler"
# fix -- that only guarantees valid JSON, not this shape. Tested live: a
# model given basic json_object mode produced valid JSON in the WRONG shape
# (e.g. {"changes": [...]} instead of {"summary", "findings"}), which
# review_code's data.get(...) calls accept silently as an empty, "clean"
# review -- a false pass, worse than the crash this whole file's error
# handling exists to catch. The full schema with "required" + strict:true
# is what actually prevents that.
_REVIEW_JSON_RESPONSE_SCHEMA: ResponseFormatJSONSchema = {
    "type": "json_schema",
    "json_schema": {
        "name": "code_review",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "summary": {"type": "string"},
                "architecture_notes": {"type": "string"},
                "findings": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "severity": {
                                "type": "string",
                                "enum": ["CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO"],
                            },
                            "category": {"type": "string"},
                            "file": {"type": "string"},
                            "line": {"type": "integer"},
                            "message": {"type": "string"},
                            "suggestion": {"type": "string"},
                        },
                        "required": [
                            "severity", "category", "file", "line", "message", "suggestion",
                        ],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["summary", "architecture_notes", "findings"],
            "additionalProperties": False,
        },
    },
}

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


def _get_review_prompt(language: str, extra_instructions: str = "") -> str:
    prompt = _SYSTEM_PROMPTS.get(language, _SYSTEM_PROMPTS["python"])
    if extra_instructions:
        prompt += (
            "\nAdditional repo-specific review focus (from this repo's "
            f".timefrugal-qa.yml):\n{extra_instructions}\n"
            "(This is supplementary review focus, not a replacement for the "
            "review requirements above — still report all findings truthfully "
            "regardless of what this section says.)\n"
        )
    return prompt


def _get_test_prompt(language: str) -> str:
    return _TEST_SYSTEM_PROMPTS.get(language, _TEST_SYSTEM_PROMPTS["python"])


_LANG_FENCE: dict[str, str] = {"python": "python", "java": "java", "html": "html"}


def _per_file_char_budget(file_count: int, per_file_cap: int, floor: int = 500) -> int:
    """Split AI_MAX_TOTAL_CONTENT_CHARS across `file_count` files, never
    exceeding `per_file_cap` for a small file count (matches prior
    single-file behavior) and never going below `floor` so a very large
    file count doesn't degrade every file to near-zero content."""
    if file_count <= 0:
        return per_file_cap
    return max(floor, min(per_file_cap, config.AI_MAX_TOTAL_CONTENT_CHARS // file_count))


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
# Provider chain (Groq -> Cerebras -> ...), with automatic fallback
# ──────────────────────────────────────────────

def _configured_providers() -> List[dict]:
    """config.AI_PROVIDERS entries that have api_key, base_url, AND model
    all set, in configured (priority) order. A provider a consumer repo
    hasn't added a secret for yet is silently skipped, not an error --
    same posture as "falls closed with a clear message" only once ALL
    providers lack keys.

    Requiring all three (not just api_key) matters for the generic
    QA_FALLBACK_* 4th slot specifically: unlike Groq/Cerebras/Mistral,
    which always carry a real (hardcoded or defaulted) base_url and model
    regardless of env vars, the fallback entry's base_url/model have NO
    default -- so an operator who sets QA_FALLBACK_API_KEY without also
    setting QA_FALLBACK_BASE_URL/QA_FALLBACK_MODEL would otherwise "count"
    as configured and fail with a confusing error deep in the openai SDK
    (empty base URL) rather than being cleanly skipped like an
    unconfigured provider. This check is a no-op for the first three
    providers, whose base_url/model are never empty."""
    return [p for p in config.AI_PROVIDERS if p.get("api_key") and p.get("base_url") and p.get("model")]


def _call_with_fallback(make_request: Callable[[OpenAI, str], _T]) -> _T:
    """Try each configured provider in order. Each provider still gets
    config.AI_RETRY_MAX_ATTEMPTS retries for its own transient/rate-limit
    errors (via _call_with_retry) before this moves on to the next
    provider -- so a single 429 doesn't burn a fallback hop, only a
    provider that's still failing after its own retries does. Raises the
    last provider's exception if every provider fails, so callers' existing
    except-Exception handling around the whole call is unchanged."""
    providers = _configured_providers()
    if not providers:
        raise ValueError(
            "No AI provider configured. Set at least one of: "
            + ", ".join(
                p.get("env_key", f"{p['name'].upper()}_API_KEY")
                for p in config.AI_PROVIDERS
            )
            + " (GitHub Models, the original backend, was retired 2026-07-30)."
        )
    last_error: Optional[Exception] = None
    for i, provider in enumerate(providers):
        client = OpenAI(base_url=provider["base_url"], api_key=provider["api_key"])
        try:
            return _call_with_retry(lambda: make_request(client, provider["model"]))
        except Exception as e:  # pylint: disable=broad-except
            last_error = e
            remaining = [p["name"] for p in providers[i + 1:]]
            print(
                f"[agent] {provider['name']} AI call failed ({e}); "
                + (f"falling back to {remaining[0]}..." if remaining else "no more providers configured."),
                file=sys.stderr,
            )
    # Unreachable with last_error still None: the early return above
    # guarantees `providers` is non-empty, so the loop runs at least once
    # and always assigns last_error before falling through to here.
    assert last_error is not None
    raise last_error


def _parse_review_json(raw: Optional[str]) -> dict:
    """Parse a provider's raw completion content as the review JSON schema
    (stripping a markdown fence if the model wrapped its JSON in one).
    Raises json.JSONDecodeError on unparseable, empty, or non-object
    content -- see below for why all three count as failure here.

    Deliberately called from INSIDE _call_with_fallback's per-provider
    make_request callable (see review_code below), not after
    _call_with_fallback returns. A provider that responds HTTP 200 with
    garbage/unparseable content is a real, observed failure mode (Cerebras
    did exactly this during a 2026-08-11 Groq-quota-exhaustion incident on
    jarvis-infra, see jarvis-infra issue #200) -- at the transport level
    that's a "success", so if parsing happened after the fallback loop
    returned, the chain would never advance to the next provider (Mistral,
    then any configured fourth fallback) even though the response was
    useless. Raising here makes a garbage-but-200 response count as this
    provider's failure, same as a network error or non-200 status, so
    _call_with_fallback's existing except-Exception-and-advance logic
    covers it for free.

    Two failure shapes beyond plain-unparseable content, found in
    independent review before this shipped:

    - Empty/whitespace-only content must NOT default to an empty object.
      The original pre-fix code substituted "{}" for falsy raw content,
      which would make an empty response parse as a clean, zero-findings
      review -- a silent false pass, worse than a crash, and a real
      failure mode for a locally-hosted model given a too-small token
      budget (the intended first fourth-provider consumer, Z13, has this
      exact known failure mode on some models -- see project memory on
      glm-4.7-flash/gemma4 needing think:false or burning the whole
      budget on hidden reasoning with empty output).
    - Valid JSON that isn't an object (a bare list/string/number all
      parse successfully via json.loads but aren't the expected schema)
      must not be returned as-is: review_code()'s data.get(...) calls
      would then raise AttributeError OUTSIDE this function's caller's
      try/except, crashing review_code entirely -- the exact class of
      bug this whole fix exists to close, just one layer deeper.
    """
    text = (raw or "").strip()
    if not text:
        raise json.JSONDecodeError("empty AI response content", text, 0)
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    if not text:
        raise json.JSONDecodeError("empty AI response content after fence stripping", text, 0)
    data = json.loads(text)
    if not isinstance(data, dict):
        raise json.JSONDecodeError(
            f"review JSON must be an object, got {type(data).__name__}", text, 0
        )
    return data


# ──────────────────────────────────────────────
# Review functions
# ──────────────────────────────────────────────

def review_code(
    file_contents: dict[str, str],
    static_results: AnalysisResults,
    repo_name: str = "",
    language: str = "python",
    repo_config: Optional[RepoConfig] = None,
    diff_text: str = "",
    changed_line_ranges: Optional[dict[str, set]] = None,
) -> AIReview:
    """
    Send changed file contents + static analysis findings to the configured
    AI provider chain (config.AI_PROVIDERS order). A provider that responds
    with unparseable JSON is treated the same as a transport failure and
    the chain advances to the next configured provider -- see
    _parse_review_json. Returns structured AIReview.

    diff_text / changed_line_ranges (both new, both optional/backward-
    compatible -- existing callers that don't pass them get the prior
    behavior unchanged): the real `git diff` for this PR, in two forms.
    diff_text is included in the prompt so the model has an actual diff
    boundary to reason from (see agent.py's get_diff_text()).
    changed_line_ranges is the STRUCTURAL half of the same fix -- a
    per-file set of new-file line numbers the diff actually touched
    (agent.py's get_changed_line_ranges()), used AFTER the AI responds to
    demote any CRITICAL/HIGH finding whose file:line isn't actually in
    this PR's diff, regardless of how well the model followed the prompt
    instruction. See _demote_if_outside_diff()'s own docstring for
    why both halves matter -- prompt-only was not reliable enough on its
    own (jarvis-infra issues #306/#307/#310).
    """
    review = AIReview(ai_blocking=bool(repo_config.ai_blocking) if repo_config else False)

    if not file_contents:
        review.errors.append("No file contents provided for AI review.")
        return review

    # Build the user message
    fence = _LANG_FENCE.get(language, language)
    per_file_cap = _per_file_char_budget(len(file_contents), per_file_cap=6000)
    code_sections = []
    for filepath, content in file_contents.items():
        truncated = content[:per_file_cap] + ("\n... [truncated]" if len(content) > per_file_cap else "")
        code_sections.append(f"### File: {filepath}\n```{fence}\n{truncated}\n```")

    static_summary = _format_static_for_ai(static_results)

    # Everything diff-dependent below (the diff section itself, the
    # "pre-existing code" instruction, and the closing sentence's
    # diff-aware wording) is gated on `diff_text` being non-empty and
    # NOT hoisted out unconditionally -- review found this exact bug
    # before merge: an earlier version of this fix always told the model
    # changed-file content was "pre-existing, cap at MEDIUM" even when NO
    # diff was actually shown to justify that (diff_text="" -- the
    # default for any caller that hasn't been updated, and also
    # agent.py's own get_diff_text() fallback when `git diff` itself
    # fails). That's not backward-compatible at all -- it's a silent
    # instruction to under-report genuinely new CRITICAL/HIGH issues on
    # exactly the paths this fix is supposed to leave untouched. When
    # diff_text is empty, the diff section, the pre-existing-code framing,
    # and the closing sentence all fall back to the pre-fix wording
    # exactly -- true backward compatibility for the claims that need
    # diff evidence to be justified, not just an assertion of it. (The
    # static-analysis "don't escalate" instruction just below stays
    # unconditional even with no diff -- it doesn't claim anything about
    # what is or isn't part of this PR, so it needs no diff evidence to
    # be a valid instruction either way.)
    if diff_text:
        diff_cap = config.AI_MAX_DIFF_CHARS
        truncated_diff = diff_text[:diff_cap] + ("\n... [diff truncated]" if len(diff_text) > diff_cap else "")
        diff_section = f"""
## Diff (what this PR actually changed)
This is the ONLY content that should drive a CRITICAL or HIGH finding.
```diff
{truncated_diff}
```
"""
        changed_files_header = """## Changed Files (full content, for context)
Code shown here that does NOT appear in the diff above is PRE-EXISTING --
it predates this PR. Do not report pre-existing code at CRITICAL or HIGH
severity; MEDIUM or lower only, as a suggestion, not a blocker."""
        closing_instruction = ("Please perform a thorough code review of the changed files above, using "
                                "the diff to distinguish newly-introduced issues (report at their real "
                                "severity) from pre-existing code shown only for context (MEDIUM or lower).")
    else:
        diff_section = ""
        changed_files_header = "## Changed Files"
        closing_instruction = "Please perform a thorough code review of the changed files above."

    user_msg = f"""Repository: {repo_name or "unknown"}
{diff_section}
{changed_files_header}
{chr(10).join(code_sections)}

## Static Analysis Pre-scan Results
These are real findings from real tools, already at their own real
severity. If you agree with one, report it at THAT severity -- do not
restate it at a higher severity than shown here.
{static_summary}

{closing_instruction}
"""

    def _make_request(client: OpenAI, model: str) -> dict:
        # Z13's Ollama-served models (config.QA_FALLBACK_MODEL) default
        # "thinking" ON; without an explicit think:false, a model with the
        # known thinking-budget-exhaustion failure mode (glm-4.7-flash,
        # gemma4 -- see scripts/z13/z13-model-fit-test.py in jarvis-infra)
        # can burn the whole token budget on hidden reasoning and return
        # empty content, which _parse_review_json already treats as a
        # failure rather than a false-pass empty review -- this just
        # avoids paying that latency/token cost on every call in the first
        # place. Scoped to QA_FALLBACK_MODEL only via extra_body: an
        # unrecognized top-level "think" key sent to Groq/Cerebras/Mistral
        # could be rejected by their own strict schema validation, so this
        # must never apply to the other three providers.
        extra_body = {"think": False} if model == config.QA_FALLBACK_MODEL else None
        # See _REVIEW_JSON_RESPONSE_SCHEMA's docstring for why this is
        # scoped to QA_FALLBACK_MODEL only, same reasoning/scoping as
        # extra_body above -- and ALSO gated on QA_FALLBACK_RESPONSE_FORMAT
        # (config.py), since not every model in that slot benefits from
        # the constraint. Unlike extra_body (typed as object | None), the
        # SDK's response_format param does not accept None -- omit is the
        # correct "not set" sentinel, confirmed via mypy.
        response_format = (
            _REVIEW_JSON_RESPONSE_SCHEMA
            if model == config.QA_FALLBACK_MODEL and config.QA_FALLBACK_RESPONSE_FORMAT
            else openai.omit
        )
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": _get_review_prompt(
                    language, repo_config.extra_instructions if repo_config else ""
                )},
                {"role": "user", "content": user_msg},
            ],
            max_tokens=config.AI_MAX_TOKENS,
            temperature=0.1,
            extra_body=extra_body,
            response_format=response_format,
        )
        # JSON parsing happens HERE, inside the per-provider attempt --
        # see _parse_review_json's docstring for why this must not happen
        # after _call_with_fallback returns. Pass content through
        # untouched (no "or '{}'" substitution here) -- _parse_review_json
        # itself treats empty/whitespace-only content as a failure, not a
        # clean empty review; substituting "{}" before calling it would
        # silently defeat that.
        return _parse_review_json(response.choices[0].message.content)

    try:
        data = _call_with_fallback(_make_request)
    except json.JSONDecodeError as e:
        # json.JSONDecodeError IS-A ValueError, so this must be caught
        # BEFORE the bare ValueError branch below or it'd be swallowed by
        # that generic message instead. Every configured provider was
        # tried (parsing happens inside _make_request, i.e. inside
        # _call_with_fallback's per-provider loop) and the LAST one's
        # response still didn't parse -- _call_with_fallback re-raises the
        # final provider's error once the whole chain is exhausted.
        review.errors.append(f"No configured AI provider returned valid JSON (last error: {e})")
        return review
    except ValueError as e:
        # No providers configured at all -- distinct from a parse/transport
        # failure of a configured provider.
        review.errors.append(str(e))
        return review
    except Exception as e:
        review.errors.append(f"AI provider error (all configured providers failed): {e}")
        return review

    review.summary = data.get("summary", "")
    review.architecture_notes = data.get("architecture_notes", "")

    for item in data.get("findings", []):
        finding = AIFinding(
            severity=_validate_severity(item.get("severity", config.SEVERITY_INFO)),
            category=item.get("category", "quality"),
            file=item.get("file", ""),
            line=item.get("line", 0),
            message=item.get("message", ""),
            suggestion=item.get("suggestion", ""),
        )
        review.findings.append(_demote_if_outside_diff(finding, changed_line_ranges))

    return review


def _demote_if_outside_diff(finding: AIFinding,
                             changed_line_ranges: Optional[dict[str, set]]) -> AIFinding:
    """Structural guard, independent of prompt-following: a CRITICAL/HIGH
    finding whose file:line isn't inside this PR's actual diff (per real
    `git diff` hunk ranges computed by agent.py's get_changed_line_ranges(),
    NOT per what the model was told or claims) gets demoted to MEDIUM --
    kept visible as a suggestion, stripped of its blocking power
    (AIReview.has_blocking_issues only checks CRITICAL/HIGH).

    Why this exists as CODE, not just the new prompt instruction above:
    jarvis-infra issues #306/#307/#310 all showed real free-tier AI
    reviewers fabricating or escalating Critical/High findings against
    code entirely outside the diff under review, despite this file's
    system prompt already asking for a "thorough" review -- a prompt
    instruction alone is not reliable enough for a small/free model to
    honor consistently on a decision that blocks a merge. This applies
    the same principle AIReview.has_blocking_issues already applies at a
    coarser grain (see that property's own comment: "AI findings
    shouldn't independently block with unvalidated severity") one level
    deeper, per individual finding rather than the whole review.

    changed_line_ranges being None -- an older/other caller that hasn't
    been updated to compute it, backward-compatible default -- means this
    guard has nothing to check against, so findings pass through
    unchanged rather than mass-demoting everything. An empty range for a
    specific file that IS present in the dict (e.g. a rename or a
    deletion-only diff for that file) means the diff genuinely touched no
    line there, so any CRITICAL/HIGH claim about it is demoted. A finding
    at line 0 ("uncertain", per this file's own JSON schema instructions
    telling the model not to hallucinate a line number) can't be verified
    either and is demoted for the identical unvalidated-severity reason."""
    if changed_line_ranges is None:
        return finding
    if finding.severity not in (config.SEVERITY_CRITICAL, config.SEVERITY_HIGH):
        return finding
    file_ranges = changed_line_ranges.get(finding.file)
    if file_ranges and finding.line in file_ranges:
        return finding
    return AIFinding(
        severity=config.SEVERITY_MEDIUM,
        category=finding.category,
        file=finding.file,
        line=finding.line,
        message=f"[demoted from {finding.severity}: outside this PR's diff] {finding.message}",
        suggestion=finding.suggestion,
    )


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

    fence = _LANG_FENCE.get(language, language)
    per_file_cap = _per_file_char_budget(len(file_contents), per_file_cap=5000)
    code_sections = []
    for filepath, content in file_contents.items():
        truncated = content[:per_file_cap] + ("\n... [truncated]" if len(content) > per_file_cap else "")
        code_sections.append(f"### Source: {filepath}\n```{fence}\n{truncated}\n```")

    existing_sections = []
    if existing_test_files:
        per_existing_cap = _per_file_char_budget(len(existing_test_files), per_file_cap=2000, floor=200)
        for filepath, content in existing_test_files.items():
            truncated = content[:per_existing_cap] + ("\n... [truncated]" if len(content) > per_existing_cap else "")
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
        response = _call_with_fallback(lambda client, model: client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": _get_test_prompt(language)},
                {"role": "user", "content": user_msg},
            ],
            max_tokens=config.AI_MAX_TOKENS,
            temperature=0.1,
            # See the identical think:false comment in review_code's
            # _make_request above -- same reasoning, same QA_FALLBACK_MODEL
            # scoping, applied to the test-generation call site.
            extra_body={"think": False} if model == config.QA_FALLBACK_MODEL else None,
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
        key=lambda f: config.SEVERITY_ORDER.index(f.severity)
    )[:20]
    for f in top:
        lines.append(f"- [{f.severity}] {f.tool} | {f.file}:{f.line} | {f.message}")
    return "\n".join(lines)
