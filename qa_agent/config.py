"""
Timefrugal-QA Agent Configuration
"""
import os

# ──────────────────────────────────────────────
# GitHub Models (free AI — requires GITHUB_TOKEN)
# ──────────────────────────────────────────────
GITHUB_MODELS_BASE_URL = "https://models.inference.ai.azure.com"

# Default model: gpt-4o-mini is free, fast, and capable enough for code review.
# Switch to "gpt-4o" for deeper analysis (still free, lower rate limit).
AI_MODEL = os.getenv("QA_AI_MODEL", "gpt-4o-mini")

# Max tokens for AI responses (keep low to stay within free rate limits)
AI_MAX_TOKENS = int(os.getenv("QA_AI_MAX_TOKENS", "3000"))

# Retry settings for GitHub Models rate-limit errors (HTTP 429)
AI_RETRY_MAX_ATTEMPTS = int(os.getenv("QA_AI_RETRY_MAX_ATTEMPTS", "3"))
AI_RETRY_BASE_DELAY = float(os.getenv("QA_AI_RETRY_BASE_DELAY", "5.0"))  # seconds; doubles each attempt

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
SUPPORTED_EXTENSIONS = PYTHON_EXTENSIONS | JAVA_EXTENSIONS | HTML_EXTENSIONS

# Java static analysis (PMD 7+)
PMD_CMD = "pmd"
# HTML linting
HTMLHINT_CMD = "htmlhint"

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
