"""
Local terminal reporter — pretty-prints review results when running outside GitHub Actions.
"""
from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich import box
from rich.text import Text
from rich.syntax import Syntax

from qa_agent import config
from qa_agent.ai_review import AIReview
from qa_agent.static_analysis import AnalysisResults

# legacy_windows=False forces ANSI mode on Windows, avoiding cp1252 UnicodeEncodeError with emoji
console = Console(legacy_windows=False)

SEVERITY_STYLE = {
    "CRITICAL": "bold red",
    "HIGH": "bold orange1",
    "MEDIUM": "bold yellow",
    "LOW": "bold cyan",
    "INFO": "dim white",
}

SEVERITY_EMOJI = {
    "CRITICAL": "🔴",
    "HIGH": "🟠",
    "MEDIUM": "🟡",
    "LOW": "🔵",
    "INFO": "⚪",
}


def print_report(
    static_results: AnalysisResults,
    ai_review: AIReview,
    generated_tests: str = "",
    changed_files: Optional[list] = None,
) -> None:
    """Print a rich, colored QA report to the terminal."""
    blocked = static_results.has_blocking_issues or ai_review.has_blocking_issues

    # Header panel
    status_text = "BLOCKED — Fix critical/high issues before raising a PR" if blocked else "PASSED — Safe to raise a PR"
    status_style = "bold red" if blocked else "bold green"
    console.print(Panel(
        Text(f"Timefrugal-QA  ·  {status_text}", style=status_style),
        box=box.DOUBLE,
    ))

    # Changed files
    if changed_files:
        console.print(f"\n[bold]Files reviewed:[/bold] {', '.join(changed_files)}\n")

    # AI Summary
    if ai_review.summary:
        console.print(Panel(ai_review.summary, title="📋 AI Summary", border_style="blue"))

    # Static analysis table
    s = static_results.summary()
    table = Table(title="🔍 Static Analysis Results", box=box.ROUNDED)
    table.add_column("Severity", style="bold")
    table.add_column("Count", justify="right")
    table.add_row("🔴 Critical", str(s["CRITICAL"]), style="bold red" if s["CRITICAL"] else "dim")
    table.add_row("🟠 High",     str(s["HIGH"]),     style="bold orange1" if s["HIGH"] else "dim")
    table.add_row("🟡 Medium",   str(s["MEDIUM"]),   style="bold yellow" if s["MEDIUM"] else "dim")
    table.add_row("🔵 Low",      str(s["LOW"]),      style="bold cyan" if s["LOW"] else "dim")
    table.add_row("⚪ Info",     str(s["INFO"]),     style="dim")
    console.print(table)

    # Static findings detail
    if static_results.findings:
        console.print("\n[bold]Static Analysis Findings:[/bold]")
        for f in sorted(static_results.findings,
                        key=lambda x: config.SEVERITY_ORDER.index(x.severity)):
            emoji = SEVERITY_EMOJI.get(f.severity, "⚪")
            style = SEVERITY_STYLE.get(f.severity, "dim")
            console.print(
                f"  {emoji} [{style}]{f.severity}[/{style}]  "
                f"[dim]{f.tool}[/dim]  {f.file}:{f.line}  {f.message}"
            )

    # AI findings
    if ai_review.findings:
        console.print("\n[bold]🤖 AI Code Review Findings:[/bold]")
        for af in sorted(ai_review.findings,
                        key=lambda x: config.SEVERITY_ORDER.index(x.severity)):
            emoji = SEVERITY_EMOJI.get(af.severity, "⚪")
            style = SEVERITY_STYLE.get(af.severity, "dim")
            loc = f"{af.file}:{af.line}" if af.line else af.file
            console.print(f"\n  {emoji} [{style}]{af.severity} / {af.category}[/{style}]  [dim]{loc}[/dim]")
            console.print(f"    Issue: {af.message}")
            if af.suggestion:
                console.print(f"    [green]Fix:[/green] {af.suggestion}")

    # Architecture notes
    if ai_review.architecture_notes:
        console.print(Panel(
            ai_review.architecture_notes,
            title="🏗️ Architecture & Design Notes",
            border_style="magenta",
        ))

    # Generated tests
    if generated_tests and generated_tests.strip():
        console.print(Panel(
            Syntax(generated_tests.strip(), "python", theme="monokai", line_numbers=True),
            title="🧪 AI-Generated Test Cases",
            border_style="green",
        ))

    # Tool errors
    all_errors = static_results.errors + ai_review.errors
    if all_errors:
        console.print("\n[bold yellow]⚙️ Tool Warnings (some tools may not be installed):[/bold yellow]")
        for e in all_errors:
            console.print(f"  [dim]  {e}[/dim]")

    # Final verdict
    verdict = "[bold red]❌ BLOCKED — fix the issues above before raising a PR[/bold red]" \
        if blocked else "[bold green]✅ All checks passed — safe to raise a PR[/bold green]"
    console.print(f"\n{verdict}\n")

    if all_errors:
        console.print(
            f"[bold yellow]⚠ {len(all_errors)} analysis tool(s) failed to run locally "
            f"— this result may not reflect what CI finds. See Tool Warnings above.[/bold yellow]\n"
        )


def save_report(
    static_results: AnalysisResults,
    ai_review: AIReview,
    generated_tests: str = "",
    output_path: Optional[str] = None,
) -> str:
    """Save a markdown report to disk. Returns the file path."""
    path = output_path or config.LOCAL_REPORT_FILE
    blocked = static_results.has_blocking_issues or ai_review.has_blocking_issues

    lines = [
        "# Timefrugal-QA Report",
        "",
        f"**Status:** {'🔴 BLOCKED' if blocked else '✅ PASSED'}",
        "",
    ]

    if ai_review.summary:
        lines += ["## Summary", ai_review.summary, ""]

    # Static findings
    s = static_results.summary()
    lines += [
        "## Static Analysis",
        f"Critical: {s['CRITICAL']} | High: {s['HIGH']} | Medium: {s['MEDIUM']} | Low: {s['LOW']}",
        "",
    ]
    for f in static_results.findings:
        lines.append(f"- **[{f.severity}]** `{f.file}:{f.line}` ({f.tool}) — {f.message}")
    lines.append("")

    # AI findings
    if ai_review.findings:
        lines.append("## AI Review Findings")
        for af in ai_review.findings:
            lines.append(f"- **[{af.severity} / {af.category}]** `{af.file}:{af.line}` — {af.message}")
            if af.suggestion:
                lines.append(f"  - Fix: {af.suggestion}")
        lines.append("")

    if ai_review.architecture_notes:
        lines += ["## Architecture Notes", ai_review.architecture_notes, ""]

    if generated_tests and generated_tests.strip():
        lines += [
            "## Generated Tests",
            "```python",
            generated_tests.strip(),
            "```",
            "",
        ]

    Path(path).write_text("\n".join(lines), encoding="utf-8")
    return path
