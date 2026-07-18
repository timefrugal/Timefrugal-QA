"""
Static analysis runner — executes bandit, semgrep, pylint, mypy, radon, pip-audit.
Returns structured findings; never raises on tool failure (graceful degradation).
"""
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from qa_agent import config
from qa_agent.repo_config import RepoConfig, filter_ignored

_SEMGREP_RULES_DIR = Path(__file__).parent / "semgrep_rules"


def detect_language(files: List[str]) -> str:
    """Return the dominant language ('python', 'java', or 'html') in the file list."""
    counts: dict[str, int] = {"python": 0, "java": 0, "html": 0}
    for f in files:
        ext = Path(f).suffix.lower()
        if ext in config.PYTHON_EXTENSIONS:
            counts["python"] += 1
        elif ext in config.JAVA_EXTENSIONS:
            counts["java"] += 1
        elif ext in config.HTML_EXTENSIONS:
            counts["html"] += 1
    return max(counts, key=counts.get) if any(counts.values()) else "python"


@dataclass
class Finding:
    tool: str
    severity: str          # CRITICAL / HIGH / MEDIUM / LOW / INFO
    category: str          # e.g. "security", "quality", "complexity"
    file: str
    line: int
    message: str
    rule_id: str = ""
    context: str = ""      # surrounding code snippet (optional)


@dataclass
class AnalysisResults:
    findings: List[Finding] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)   # tool execution errors
    # Resolved effective threshold for this run (repo config takes precedence
    # over the QA_BLOCK_MERGE_THRESHOLD env var / hardcoded default). None
    # means "use config.BLOCK_MERGE_THRESHOLD".
    block_merge_threshold: Optional[str] = None

    @property
    def has_blocking_issues(self) -> bool:
        threshold_order = config.SEVERITY_ORDER
        threshold = self.block_merge_threshold or config.BLOCK_MERGE_THRESHOLD
        cutoff = threshold_order.index(threshold)
        for f in self.findings:
            # Radon complexity findings are advisory-only: reported for
            # visibility but never gate a merge on their own.
            if f.category == "complexity":
                continue
            if f.severity in threshold_order[:cutoff + 1]:
                return True
        return False

    def by_severity(self, severity: str) -> List[Finding]:
        return [f for f in self.findings if f.severity == severity]

    def summary(self) -> dict:
        counts = {}
        for sev in config.SEVERITY_ORDER:
            counts[sev] = len(self.by_severity(sev))
        return counts


# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────

def _run(cmd: List[str], cwd: Optional[str] = None) -> tuple[int, str, str]:
    """Run a subprocess and return (returncode, stdout, stderr)."""
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=cwd,
        )
        return result.returncode, result.stdout, result.stderr
    except FileNotFoundError:
        return -1, "", f"Tool not found: {cmd[0]}"


def _bandit_severity(bandit_sev: str) -> str:
    mapping = {"HIGH": config.SEVERITY_HIGH, "MEDIUM": config.SEVERITY_MEDIUM, "LOW": config.SEVERITY_LOW}
    return mapping.get(bandit_sev.upper(), config.SEVERITY_LOW)


def _semgrep_severity(sg_sev: str) -> str:
    mapping = {
        "ERROR": config.SEVERITY_CRITICAL,
        # WARNING historically mapped to HIGH, which made semgrep's noisy
        # community ruleset independently block merges. MEDIUM is the new
        # unconditional default (H2: severity mapping systematically over-blocks).
        "WARNING": config.SEVERITY_MEDIUM,
        "INFO": config.SEVERITY_MEDIUM,
    }
    return mapping.get(sg_sev.upper(), config.SEVERITY_INFO)


def _pmd_severity(priority: int) -> str:
    # PMD priority: 1=Critical, 2=High, 3=Medium, 4=Low, 5=Info
    return {1: config.SEVERITY_CRITICAL, 2: config.SEVERITY_HIGH,
            3: config.SEVERITY_MEDIUM, 4: config.SEVERITY_LOW}.get(priority, config.SEVERITY_INFO)


def _pylint_severity(msg_type: str, overrides: Optional[Dict] = None) -> str:
    key = msg_type.upper()[:1]
    if overrides and key in overrides:
        return overrides[key]
    mapping = {
        "F": config.SEVERITY_CRITICAL,   # fatal
        "E": config.SEVERITY_HIGH,        # error
        "W": config.SEVERITY_MEDIUM,      # warning
        "R": config.SEVERITY_LOW,         # refactor
        "C": config.SEVERITY_INFO,        # convention
    }
    return mapping.get(key, config.SEVERITY_INFO)


# ──────────────────────────────────────────────
# Individual tool runners
# ──────────────────────────────────────────────

def run_bandit(files: List[str]) -> AnalysisResults:
    """Run bandit security linter on given Python files."""
    results = AnalysisResults()
    if not files:
        return results

    cmd = [config.BANDIT_CMD, "-f", "json", "-q"] + files
    rc, stdout, stderr = _run(cmd)

    if rc == -1:
        results.errors.append(f"bandit: {stderr}")
        return results

    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        results.errors.append(f"bandit: could not parse output")
        return results

    for issue in data.get("results", []):
        results.findings.append(Finding(
            tool="bandit",
            severity=_bandit_severity(issue.get("issue_severity", "LOW")),
            category="security",
            file=issue.get("filename", ""),
            line=issue.get("line_number", 0),
            message=issue.get("issue_text", ""),
            rule_id=issue.get("test_id", ""),
            context=issue.get("code", ""),
        ))

    return results


def run_semgrep(files: List[str]) -> AnalysisResults:
    """Run semgrep with the free community ruleset."""
    results = AnalysisResults()
    if not files:
        return results

    cmd = [
        config.SEMGREP_CMD, "scan",
        "--config", "auto",      # free community rules
        "--json",
        "--quiet",
    ]
    if _SEMGREP_RULES_DIR.is_dir():
        cmd += ["--config", str(_SEMGREP_RULES_DIR)]
    cmd += files
    rc, stdout, stderr = _run(cmd)

    if rc == -1:
        results.errors.append(f"semgrep: {stderr}")
        return results

    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        results.errors.append("semgrep: could not parse output")
        return results

    for item in data.get("results", []):
        extra = item.get("extra", {})
        results.findings.append(Finding(
            tool="semgrep",
            severity=_semgrep_severity(extra.get("severity", "INFO")),
            category=extra.get("metadata", {}).get("category", "security"),
            file=item.get("path", ""),
            line=item.get("start", {}).get("line", 0),
            message=extra.get("message", ""),
            rule_id=item.get("check_id", ""),
            context=extra.get("lines", ""),
        ))

    return results


def run_pylint(files: List[str], repo_config: Optional[RepoConfig] = None) -> AnalysisResults:
    """Run pylint for code quality and bug detection."""
    results = AnalysisResults()
    if not files:
        return results

    overrides = (
        repo_config.severity_overrides.get("pylint", {}) if repo_config else {}
    )

    cmd = [
        config.PYLINT_CMD,
        "--output-format=json",
        "--disable=C0114,C0115,C0116",   # suppress missing-docstring for speed
    ] + files
    rc, stdout, stderr = _run(cmd)

    if rc == -1:
        results.errors.append(f"pylint: {stderr}")
        return results

    try:
        items = json.loads(stdout)
    except json.JSONDecodeError:
        results.errors.append("pylint: could not parse output")
        return results

    for item in items:
        msg_type = item.get("type", "C")
        results.findings.append(Finding(
            tool="pylint",
            severity=_pylint_severity(msg_type, overrides),
            category="quality",
            file=item.get("path", ""),
            line=item.get("line", 0),
            message=f"[{item.get('message-id', '')}] {item.get('message', '')}",
            rule_id=item.get("message-id", ""),
        ))

    return results


def run_mypy(files: List[str], repo_config: Optional[RepoConfig] = None) -> AnalysisResults:
    """Run mypy for type checking."""
    results = AnalysisResults()
    if not files:
        return results

    overrides = (
        repo_config.severity_overrides.get("mypy", {}) if repo_config else {}
    )

    cmd = [config.MYPY_CMD, "--no-error-summary", "--show-column-numbers"] + files
    rc, stdout, stderr = _run(cmd)

    if rc == -1:
        results.errors.append(f"mypy: {stderr}")
        return results

    for line in stdout.splitlines():
        # Format: file.py:10:5: error: message  [error-code]
        parts = line.split(":", 4)
        if len(parts) >= 4:
            sev_word = parts[3].strip().lower()
            if sev_word in overrides:
                severity = overrides[sev_word]
            else:
                severity = (
                    config.SEVERITY_HIGH if sev_word == "error"
                    else config.SEVERITY_MEDIUM if sev_word == "warning"
                    else config.SEVERITY_INFO
                )
            try:
                lineno = int(parts[1])
            except ValueError:
                lineno = 0
            results.findings.append(Finding(
                tool="mypy",
                severity=severity,
                category="types",
                file=parts[0],
                line=lineno,
                message=parts[4].strip() if len(parts) > 4 else line,
                rule_id="mypy",
            ))

    return results


def run_radon(files: List[str]) -> AnalysisResults:
    """Flag functions with high cyclomatic complexity."""
    results = AnalysisResults()
    if not files:
        return results

    cmd = [config.RADON_CMD, "cc", "-j", "-s"] + files
    rc, stdout, stderr = _run(cmd)

    if rc == -1:
        results.errors.append(f"radon: {stderr}")
        return results

    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        results.errors.append("radon: could not parse output")
        return results

    for filepath, items in data.items():
        for item in items:
            complexity = item.get("complexity", 0)
            if complexity > config.MAX_COMPLEXITY:
                results.findings.append(Finding(
                    tool="radon",
                    severity=config.SEVERITY_MEDIUM if complexity <= 20 else config.SEVERITY_HIGH,
                    category="complexity",
                    file=filepath,
                    line=item.get("lineno", 0),
                    message=(
                        f"Function '{item.get('name', '?')}' has cyclomatic complexity "
                        f"{complexity} (threshold: {config.MAX_COMPLEXITY}). Consider refactoring."
                    ),
                    rule_id="CC",
                ))

    return results


# pyproject.toml intentionally not audited here -- pip-audit's --path/project-path modes
# trigger a full build/dependency resolution of the target repo, which is slow and risks
# executing untrusted build-backend code in a shared CI tool.
_PIP_AUDIT_MANIFESTS = ["requirements.txt", "requirements-dev.txt"]

def run_pip_audit(project_root: str = ".") -> AnalysisResults:
    """Audit the target project's own declared Python dependencies for known vulnerabilities."""
    results = AnalysisResults()

    manifests = [m for m in _PIP_AUDIT_MANIFESTS if (Path(project_root) / m).is_file()]
    if not manifests:
        return results  # no manifest -> silent no-op, same pattern as run_pmd/run_htmlhint on empty input

    cmd = [config.PIP_AUDIT_CMD, "--format=json", "--progress-spinner=off"]
    for m in manifests:
        cmd += ["-r", m]
    rc, stdout, stderr = _run(cmd, cwd=project_root)

    if rc == -1:
        results.errors.append(f"pip-audit: {stderr}")
        return results

    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        results.errors.append("pip-audit: could not parse output")
        return results

    manifest_label = ", ".join(manifests)
    for dep in data.get("dependencies", []):
        for vuln in dep.get("vulns", []):
            severity_str = vuln.get("fix_versions") and config.SEVERITY_HIGH or config.SEVERITY_MEDIUM
            results.findings.append(Finding(
                tool="pip-audit",
                severity=severity_str,
                category="dependency",
                file=manifest_label,
                line=0,
                message=(
                    f"Package '{dep.get('name')}=={dep.get('version')}': "
                    f"{vuln.get('description', '')} "
                    f"(Fix: {', '.join(vuln.get('fix_versions', ['none']))})"
                ),
                rule_id=vuln.get("id", ""),
            ))

    return results


def run_pmd(files: List[str]) -> AnalysisResults:
    """Run PMD 7+ for Java static analysis (graceful degradation if not installed)."""
    results = AnalysisResults()
    if not files:
        return results

    cmd = [
        config.PMD_CMD, "check",
        "-d", ",".join(files),
        "-R", "rulesets/java/quickstart.xml",
        "-f", "json",
        "--no-progress-bar",
    ]
    rc, stdout, stderr = _run(cmd)

    if rc == -1:
        results.errors.append(f"pmd: {stderr}")
        return results

    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        results.errors.append("pmd: could not parse output")
        return results

    for file_result in data.get("files", []):
        filename = file_result.get("filename", "")
        for v in file_result.get("violations", []):
            results.findings.append(Finding(
                tool="pmd",
                severity=_pmd_severity(v.get("priority", 3)),
                category="quality",
                file=filename,
                line=v.get("beginline", 0),
                message=v.get("description", ""),
                rule_id=v.get("rule", ""),
            ))

    return results


def run_htmlhint(files: List[str]) -> AnalysisResults:
    """Run htmlhint for HTML linting (graceful degradation if not installed)."""
    results = AnalysisResults()
    if not files:
        return results

    cmd = [config.HTMLHINT_CMD, "--format", "json"] + files
    rc, stdout, stderr = _run(cmd)

    if rc == -1:
        results.errors.append(f"htmlhint: {stderr}")
        return results

    try:
        data = json.loads(stdout or "[]")
    except json.JSONDecodeError:
        results.errors.append("htmlhint: could not parse output")
        return results

    for file_result in data:
        filename = file_result.get("file", "")
        for msg in file_result.get("messages", []):
            severity = (
                config.SEVERITY_HIGH if msg.get("type") == "error"
                else config.SEVERITY_MEDIUM
            )
            results.findings.append(Finding(
                tool="htmlhint",
                severity=severity,
                category="quality",
                file=filename,
                line=msg.get("line", 0),
                message=msg.get("message", ""),
                rule_id=msg.get("rule", ""),
            ))

    return results


# ──────────────────────────────────────────────
# Orchestrator
# ──────────────────────────────────────────────

def run_all(
    files: List[str],
    project_root: str = ".",
    repo_config: Optional[RepoConfig] = None,
) -> AnalysisResults:
    """
    Run static analysis tools appropriate for the languages present in `files`.
    Supports Python (.py), Java (.java), and HTML (.html/.htm).
    Returns a merged AnalysisResults.

    `repo_config`, when provided, is used to: apply per-tool severity
    overrides (mypy/pylint), filter out waived (tool, rule_id) findings, and
    resolve the effective block-merge threshold. Omitting it (the default)
    reproduces today's behavior exactly.
    """
    combined = AnalysisResults()

    existing = [f for f in files if Path(f).exists()]
    py_files   = [f for f in existing if Path(f).suffix.lower() in config.PYTHON_EXTENSIONS]
    java_files = [f for f in existing if Path(f).suffix.lower() in config.JAVA_EXTENSIONS]
    html_files = [f for f in existing if Path(f).suffix.lower() in config.HTML_EXTENSIONS]
    all_supported = py_files + java_files + html_files

    runners: dict = {}
    if all_supported:
        runners["semgrep"] = lambda: run_semgrep(all_supported)
    if py_files:
        runners["bandit"]    = lambda: run_bandit(py_files)
        runners["pylint"]    = lambda: run_pylint(py_files, repo_config=repo_config)
        runners["mypy"]      = lambda: run_mypy(py_files, repo_config=repo_config)
        runners["radon"]     = lambda: run_radon(py_files)
        runners["pip-audit"] = lambda: run_pip_audit(project_root)
    if java_files:
        runners["pmd"] = lambda: run_pmd(java_files)
    if html_files:
        runners["htmlhint"] = lambda: run_htmlhint(html_files)

    if not runners:
        return combined

    with ThreadPoolExecutor(max_workers=len(runners)) as pool:
        futures = {pool.submit(fn): name for name, fn in runners.items()}
        for future in as_completed(futures):
            name = futures[future]
            try:
                result = future.result()
                combined.findings.extend(result.findings)
                combined.errors.extend(result.errors)
            except Exception as exc:
                combined.errors.append(f"{name}: unexpected error — {exc}")

    if repo_config is not None:
        if repo_config.ignore:
            combined.findings = filter_ignored(combined.findings, repo_config.ignore)
        if repo_config.block_merge_threshold:
            combined.block_merge_threshold = repo_config.block_merge_threshold

    return combined
