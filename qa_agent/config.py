"""
Timefrugal-QA Agent Configuration
"""
import os

# ──────────────────────────────────────────────
# AI backend — a chain of free-tier providers, tried in order
#
# GitHub Models (the original free backend) was fully retired by GitHub on
# 2026-07-30 -- playground, catalog, inference API, and BYOK all gone, for
# every customer, no exceptions. Switched to a provider chain instead of a
# single backend, since free tiers come with real rate limits (Groq's
# openai/gpt-oss-120b: 8,000 TPM) that a single moderately-sized PR
# can legitimately hit -- see ai_review.py's _call_with_fallback for the
# fallback mechanism. Every provider here is OpenAI-SDK-compatible (same
# base_url + api_key + chat.completions.create() shape), so adding another
# is just another entry in AI_PROVIDERS.
# ──────────────────────────────────────────────
AI_BASE_URL = "https://api.groq.com/openai/v1"  # kept for backward compat; AI_PROVIDERS is authoritative

# Default model: openai/gpt-oss-120b is free on Groq's tier and capable
# enough for code review. Replaced llama-3.3-70b-versatile on 2026-08-20
# (jarvis-infra issue #309): Groq retired its entire llama chat lineup, so
# the old tag now 404s outright -- tier 1 of the chain was silently dead on
# every real QA run, landing every review on Cerebras or Mistral instead.
# openai/gpt-oss-120b is Groq's own recommended general-purpose/reasoning
# replacement, and it's the same model family already used one hop down the
# chain (AI_MODEL_CEREBRAS) and by jarvis-infra's primary Ollama workhorse
# (Z13/jarvis-1 Slot A), so behavior/quality expectations are a known
# quantity. Note the "openai/" prefix: Groq namespaces this model, Cerebras
# does not (plain "gpt-oss-120b" below) -- the two strings differ on
# purpose, don't "fix" one to match the other. Tradeoff: Groq's free tier
# gives it 8,000 TPM (vs the retired model's 12,000), so tier 1 has less
# headroom than before; 429s/413s fall through to Cerebras/Mistral via
# _call_with_fallback exactly as designed. openai/gpt-oss-20b has the same
# 8,000 TPM, so downsizing would buy no headroom, only less capability.
AI_MODEL = os.getenv("QA_AI_MODEL", "openai/gpt-oss-120b")

# Cerebras free tier's current model lineup (gpt-oss-120b, production) --
# same model family already used for jarvis-infra's own primary Ollama
# workhorse (Z13/jarvis-1 Slot A) and, as of 2026-08-20, by the Groq tier
# above too, so likewise a known quantity. Cerebras' free tier has ~3.75x
# Groq's TPM ceiling (30K vs 8K) as of this writing, making it a strong
# second hop when Groq is out of quota.
AI_MODEL_CEREBRAS = os.getenv("QA_AI_MODEL_CEREBRAS", "gpt-oss-120b")

# Max tokens for AI responses (keep low to stay within free rate limits)
AI_MAX_TOKENS = int(os.getenv("QA_AI_MAX_TOKENS", "3000"))

# Total character budget for changed-file content in a single AI request,
# spread across however many files changed. A fixed per-file cap (the old
# behavior) scales unboundedly with file count -- a 9-file PR could request
# ~15,500 tokens against Groq's free-tier TPM limit (12,000 when that 413
# actually happened; 8,000 since the 2026-08-20 model swap, so the margin
# is tighter now, not looser) and 413 outright.
# review_code() and generate_tests() run concurrently (agent.py's
# ThreadPoolExecutor) and share the same per-org TPM budget within the same
# minute, so this is deliberately conservative for a single call.
AI_MAX_TOTAL_CONTENT_CHARS = int(os.getenv("QA_AI_MAX_TOTAL_CONTENT_CHARS", "16000"))

# Character budget for the unified diff text sent alongside (not instead
# of) the truncated full-file content above -- a separate, additive cap,
# not carved out of AI_MAX_TOTAL_CONTENT_CHARS. Added to fix a real,
# repeatedly-observed failure mode (jarvis-infra issues #306/#307/#310):
# without ANY diff boundary in the prompt, review_code() previously sent
# only full (truncated) file content, giving the AI reviewer no way to
# tell newly-introduced lines from code that predates the PR by months --
# it would routinely misattribute or escalate pre-existing static-analysis
# findings as fabricated new CRITICAL/HIGH issues. Diffs are normally far
# smaller than full files, so 8000 is generous for most real PRs while
# still bounded for a very large diff.
AI_MAX_DIFF_CHARS = int(os.getenv("QA_AI_MAX_DIFF_CHARS", "8000"))

# Retry settings for rate-limit errors (HTTP 429)
AI_RETRY_MAX_ATTEMPTS = int(os.getenv("QA_AI_RETRY_MAX_ATTEMPTS", "3"))
AI_RETRY_BASE_DELAY = float(os.getenv("QA_AI_RETRY_BASE_DELAY", "5.0"))  # seconds; doubles each attempt

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
CEREBRAS_API_KEY = os.getenv("CEREBRAS_API_KEY", "")
MISTRAL_API_KEY = os.getenv("MISTRAL_API_KEY", "")

# mistral-small-latest is Mistral's free "Experiment" tier model (opt-in to
# data training required to unlock the tier's full ~1B token/month quota --
# DJ's own account/tradeoff decision, not this tool's to make).
AI_MODEL_MISTRAL = os.getenv("QA_AI_MODEL_MISTRAL", "mistral-small-latest")

# Fourth, last-resort fallback slot: a generic OpenAI-SDK-compatible
# endpoint, not tied to a specific named cloud service like the three
# above. Named QA_FALLBACK_* rather than following a per-service pattern
# (e.g. QA_AI_MODEL_<PROVIDER>) because this slot isn't for a fixed
# service -- the first consumer is jarvis-infra routing to Z13 (a
# Tailscale-reachable home GPU box running an Ollama-compatible gateway)
# as a last resort when Groq/Cerebras/Mistral are all exhausted or down
# (see jarvis-infra issue #200), but the slot itself is generic. No
# built-in default for any of the three -- unlike Cerebras/Mistral's
# base_url defaults, there's no sensible default endpoint for an
# arbitrary self-hosted fallback, so all three must be explicitly set
# together or this entry stays absent. Unlike Groq/Cerebras/Mistral
# (gated purely on api_key, since their base_url/model always come from a
# real default), ai_review._configured_providers ENFORCES this for the
# generic slot specifically: it requires api_key AND base_url AND model
# all non-empty, so a partially-configured fallback (e.g. only
# QA_FALLBACK_API_KEY set) is cleanly skipped rather than "counting" as
# configured and failing confusingly deep inside the openai SDK.
QA_FALLBACK_BASE_URL = os.getenv("QA_FALLBACK_BASE_URL", "")
QA_FALLBACK_API_KEY = os.getenv("QA_FALLBACK_API_KEY", "")
QA_FALLBACK_MODEL = os.getenv("QA_FALLBACK_MODEL", "")

# Whether review_code() sends a decoding-time JSON-schema response_format
# constraint (see ai_review._REVIEW_JSON_RESPONSE_SCHEMA) on calls to
# QA_FALLBACK_MODEL. Defaults on -- this is what was verified live against
# qwen2.5:7b (a model that writes good review content but doesn't reliably
# emit valid JSON from plain-text instructions alone).
#
# Not every model that ends up in the QA_FALLBACK_MODEL slot needs or
# benefits from this, though: a real-world eval (Mika#66/#68, 2026-08-17)
# found a DIFFERENT model plugged into the same slot -- llama4:scout,
# which already emits schema-compliant JSON from the plain prompt and had
# an 8/8 unconstrained track record -- started failing 4/4 with response
# truncation once response_format was force-applied to it. The schema
# constraint changes decoding behavior in a way that isn't free for every
# model, so this must be an explicit per-deployment choice, not baked in
# unconditionally for the whole QA_FALLBACK_MODEL slot. Consumer repos
# whose QA_FALLBACK_MODEL doesn't need it should set this to "false".
QA_FALLBACK_RESPONSE_FORMAT = os.getenv(
    "QA_FALLBACK_RESPONSE_FORMAT", "true"
).strip().lower() not in ("false", "0", "no")

# Explicit reasoning-effort override for QA_FALLBACK_MODEL calls (review_code
# and generate_tests), sent as extra_body={"reasoning_effort": <value>}
# INSTEAD OF the think:false extra_body below when set. Exists because
# think:false is not honored by every model in this slot over Ollama's
# OpenAI-compat layer -- confirmed on qwen3.8:27b (Timefrugal-QA#23):
# think:false burns the entire AI_MAX_TOKENS budget on hidden reasoning and
# returns empty content (a hard parse failure), while an explicit
# reasoning_effort="low" produces real, valid output because Ollama's
# compat layer honors that param instead. Empty by default -- a deployment
# whose QA_FALLBACK_MODEL already works correctly with think:false (e.g.
# glm-4.7-flash, gemma4, this slot's original verified config) gets no
# behavior change; only one that has hit this specific gap needs to set it.
QA_FALLBACK_REASONING_EFFORT = os.getenv("QA_FALLBACK_REASONING_EFFORT", "").strip()

# Provider chain, tried in this order by ai_review._call_with_fallback. A
# provider whose API key env var isn't set is skipped, not an error --
# consumer repos can add CEREBRAS_API_KEY/MISTRAL_API_KEY/QA_FALLBACK_API_KEY
# whenever they want the fallback, and things keep working Groq-only until
# then. QA_FALLBACK_* (last) is deliberately last-resort: it's only reached
# once Groq, Cerebras, AND Mistral have all failed/are all unconfigured.
AI_PROVIDERS = [
    {
        "name": "groq",
        "base_url": AI_BASE_URL,
        "api_key": GROQ_API_KEY,
        "model": AI_MODEL,
    },
    {
        "name": "cerebras",
        "base_url": os.getenv("CEREBRAS_BASE_URL", "https://api.cerebras.ai/v1"),
        "api_key": CEREBRAS_API_KEY,
        "model": AI_MODEL_CEREBRAS,
    },
    {
        "name": "mistral",
        "base_url": os.getenv("MISTRAL_BASE_URL", "https://api.mistral.ai/v1"),
        "api_key": MISTRAL_API_KEY,
        "model": AI_MODEL_MISTRAL,
    },
    {
        "name": "fallback",
        "base_url": QA_FALLBACK_BASE_URL,
        "api_key": QA_FALLBACK_API_KEY,
        "model": QA_FALLBACK_MODEL,
        # Explicit override: ai_review's "no providers configured" error
        # message derives an env var name from f"{name.upper()}_API_KEY" by
        # default (correct for groq/cerebras/mistral), which would say
        # "FALLBACK_API_KEY" here -- wrong, the real var is
        # QA_FALLBACK_API_KEY. env_key lets a provider entry override that
        # derived name; entries without it keep the default derivation.
        "env_key": "QA_FALLBACK_API_KEY",
    },
]

# ──────────────────────────────────────────────
# GitHub API
# ──────────────────────────────────────────────
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
GITHUB_API_URL = "https://api.github.com"

# Set by GitHub Actions automatically
GITHUB_REPOSITORY = os.getenv("GITHUB_REPOSITORY", "")   # "owner/repo"
GITHUB_SHA = os.getenv("GITHUB_SHA", "")
PR_NUMBER = os.getenv("PR_NUMBER", "")

# ──────────────────────────────────────────────
# Severity levels  (used for blocking decisions)
# ──────────────────────────────────────────────
SEVERITY_CRITICAL = "CRITICAL"
SEVERITY_HIGH = "HIGH"
SEVERITY_MEDIUM = "MEDIUM"
SEVERITY_LOW = "LOW"
SEVERITY_INFO = "INFO"

# Ordered most-to-least severe. This is the single source of truth for
# severity ordering/validation -- anything that needs "all known severities"
# or "which is more severe" (threshold cutoffs, sort order, YAML config
# validation) should derive from this list rather than hardcoding its own
# copy of the five strings.
SEVERITY_ORDER = [SEVERITY_CRITICAL, SEVERITY_HIGH, SEVERITY_MEDIUM, SEVERITY_LOW, SEVERITY_INFO]

# PRs are blocked (merge prevented) if any finding at or above this level exists.
# A per-repo `.timefrugal-qa.yml` (`block_merge_threshold:`) takes precedence
# over this env var when both are present -- see qa_agent.repo_config.
BLOCK_MERGE_THRESHOLD = os.environ.get("QA_BLOCK_MERGE_THRESHOLD", SEVERITY_HIGH)

# ──────────────────────────────────────────────
# Static analysis tool paths (auto-detected from PATH)
# ──────────────────────────────────────────────
BANDIT_CMD = "bandit"
SEMGREP_CMD = "semgrep"
PYLINT_CMD = "pylint"
MYPY_CMD = "mypy"
RADON_CMD = "radon"
PIP_AUDIT_CMD = "pip-audit"

# ──────────────────────────────────────────────
# Supported languages and file extensions
# ──────────────────────────────────────────────
PYTHON_EXTENSIONS = {".py"}
JAVA_EXTENSIONS = {".java"}
HTML_EXTENSIONS = {".html", ".htm"}
JAVASCRIPT_EXTENSIONS = {".js", ".jsx", ".mjs", ".cjs"}
TYPESCRIPT_EXTENSIONS = {".ts", ".tsx"}
SUPPORTED_EXTENSIONS = (
    PYTHON_EXTENSIONS | JAVA_EXTENSIONS | HTML_EXTENSIONS
    | JAVASCRIPT_EXTENSIONS | TYPESCRIPT_EXTENSIONS
)

# Java static analysis (PMD 7+)
PMD_CMD = "pmd"
# HTML linting
HTMLHINT_CMD = "htmlhint"
# JavaScript/TypeScript linting + type checking
ESLINT_CMD = "eslint"
TSC_CMD = "tsc"

# Files/dirs to always skip
EXCLUDE_PATTERNS = [
    "migrations/",
    "venv/",
    ".venv/",
    "node_modules/",
    "__pycache__/",
    ".git/",
    "dist/",
    "build/",
    "*.egg-info/",
]

# Complexity threshold — flag functions with cyclomatic complexity above this
MAX_COMPLEXITY = int(os.getenv("QA_MAX_COMPLEXITY", "10"))

# ──────────────────────────────────────────────
# Local mode output
# ──────────────────────────────────────────────
LOCAL_REPORT_FILE = os.getenv("QA_REPORT_FILE", "qa_report.md")
