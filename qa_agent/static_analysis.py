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
from typing import List, Optional

from qa_agent import config


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

    @property
    def has_blocking_issues(self) -> bool:
        threshold_order = [
            config.SEVERITY_CRITICAL,
            config.SEVERITY_HIGH,
            config.SEVERITY_MEDIUM,
            config.SEVERITY_LOW,
            config.SEVERITY_INFO,
        ]
        cutoff = threshold_order.index(config.BLOCK_MERGE_THRESHOLD)
        for f in self.findings:
            if f.severity in threshold_order[:cutoff + 1]:
                return True
        return False

    def by_severity(self, severity: str) -> List[Finding]:
        return [f for f in self.findings if f.severity == severity]

    def summary(self) -> dict:
        counts = {}
        for sev in [
            config.SEVERITY_CRITICAL,
            config.SEVERITY_HIGH,
            config.SEVERITY_MEDIUM,
            config.SEVERITY_LOW,
            config.SEVERITY_INFO,
        ]:
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
        "WARNING": config.SEVERITY_HIGH,
        "INFO": config.SEVERITY_MEDIUM,
    }
    return mapping.get(sg_sev.upper(), config.SEVERITY_INFO)


def _pylint_severity(msg_type: str) -> str:
    mapping = {
        "F": config.SEVERITY_CRITICAL,   # fatal
        "E": config.SEVERITY_HIGH,        # error
        "W": config.SEVERITY_MEDIUM,      # warning
        "R": config.SEVERITY_LOW,         # refactor
        "C": config.SEVERITY_INFO,        # convention
    }
    return mapping.get(msg_type.upper()[:1], config.SEVERITY_INFO)


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
    ] + files
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
        results.findings.append(Finding(
            tool="semgrep",
            severity=_semgrep_severity(item.get("extra", {}).get("severity", "INFO")),
            category="security",
            file=item.get("path", ""),
            line=item.get("start", {}).get("line", 0),
            message=item.get("extra", {}).get("message", ""),
            rule_id=item.get("check_id", ""),
            context=item.get("extra", {}).get("lines", ""),
        ))

    return results


def run_pylint(files: List[str]) -> AnalysisResults:
    """Run pylint for code quality and bug detection."""
    results = AnalysisResults()
    if not files:
        return results

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
            severity=_pylint_severity(msg_type),
            category="quality",
            file=item.get("path", ""),
            line=item.get("line", 0),
            message=f"[{item.get('message-id', '')}] {item.get('message', '')}",
            rule_id=item.get("message-id", ""),
        ))

    return results


def run_mypy(files: List[str]) -> AnalysisResults:
    """Run mypy for type checking."""
    results = AnalysisResults()
    if not files:
        return results

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


def run_pip_audit(project_root: str = ".") -> AnalysisResults:
    """Audit Python dependencies for known vulnerabilities."""
    results = AnalysisResults()

    cmd = [config.PIP_AUDIT_CMD, "--format=json", "--progress-spinner=off"]
    rc, stdout, stderr = _run(cmd, cwd=project_root)

    if rc == -1:
        results.errors.append(f"pip-audit: {stderr}")
        return results

    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        results.errors.append("pip-audit: could not parse output")
        return results

    for dep in data.get("dependencies", []):
        for vuln in dep.get("vulns", []):
            severity_str = vuln.get("fix_versions") and config.SEVERITY_HIGH or config.SEVERITY_MEDIUM
            results.findings.append(Finding(
                tool="pip-audit",
                severity=severity_str,
                category="dependency",
                file="requirements.txt",
                line=0,
                message=(
                    f"Package '{dep.get('name')}=={dep.get('version')}': "
                    f"{vuln.get('description', '')} "
                    f"(Fix: {', '.join(vuln.get('fix_versions', ['none']))})"
                ),
                rule_id=vuln.get("id", ""),
            ))

    return results


# ──────────────────────────────────────────────
# Orchestrator
# ──────────────────────────────────────────────

def run_all(files: List[str], project_root: str = ".") -> AnalysisResults:
    """
    Run all static analysis tools on the given list of Python files.
    Returns a merged AnalysisResults.
    """
    combined = AnalysisResults()

    # Filter to only existing Python files
    py_files = [f for f in files if f.endswith(".py") and Path(f).exists()]

    runners = {
        "bandit":    lambda: run_bandit(py_files),
        "semgrep":   lambda: run_semgrep(py_files),
        "pylint":    lambda: run_pylint(py_files),
        "mypy":      lambda: run_mypy(py_files),
        "radon":     lambda: run_radon(py_files),
        "pip-audit": lambda: run_pip_audit(project_root),
    }

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

    return combined
