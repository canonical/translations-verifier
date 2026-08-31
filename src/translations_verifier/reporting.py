from __future__ import annotations

from pathlib import Path

from translations_verifier.models import Finding, Severity, VerificationReport


def write_json_report(report: VerificationReport, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")


def render_markdown(report: VerificationReport) -> str:
    error_count = sum(finding.severity is Severity.ERROR for finding in report.findings)
    warning_count = sum(finding.severity is Severity.WARNING for finding in report.findings)
    operation_summary = (
        f"Operations: {len(report.operations)} | Errors: {error_count} | Warnings: {warning_count} "
    )
    lines = [
        "# Translation verification",
        "",
        f"**Status:** `{report.status}`",
        "",
        f"PR `{report.repository}#{report.pull_request}` at `{report.head_sha}` ",
        f"against `{report.base_sha}`.",
        "",
        operation_summary,
        f"| AI reviews: {len(report.ai_reviews)} | Ignored files: {report.ignored_file_count}",
    ]
    if report.incomplete_reasons:
        lines.extend(["", "## Incomplete", ""])
        lines.extend(f"- {_markdown_text(reason)}" for reason in report.incomplete_reasons)
    if report.findings:
        lines.extend(
            ["", "## Findings", "", "| Severity | Rule | Location | Message |", "|---|---|---|---|"]
        )
        for finding in report.findings:
            location = finding.path
            if finding.key:
                location += f":{finding.key}"
            message = _finding_message(finding)
            lines.append(
                f"| {finding.severity} | `{_markdown_text(finding.rule_id)}` | "
                f"`{_markdown_text(location)}` | {_markdown_text(message)} |"
            )
    return "\n".join(lines) + "\n"


def render_github_annotations(report: VerificationReport) -> str:
    annotations: list[str] = []
    for finding in report.findings:
        level = "error" if finding.severity is Severity.ERROR else "warning"
        properties = [f"file={_workflow_property(finding.path)}"]
        if finding.line is not None:
            properties.append(f"line={finding.line}")
        title = _workflow_property(finding.rule_id[:100])
        message = _workflow_message(_finding_message(finding)[:1000])
        annotations.append(f"::{level} {','.join(properties)},title={title}::{message}")
    return "\n".join(annotations) + ("\n" if annotations else "")


def _markdown_text(value: str) -> str:
    return value.replace("\\", "\\\\").replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def _finding_message(finding: Finding) -> str:
    suggestion = finding.evidence.get("suggested_translation")
    if isinstance(suggestion, str) and suggestion.strip():
        return f"{finding.message} Suggested translation: {suggestion}"
    return finding.message


def _workflow_property(value: str) -> str:
    return _workflow_message(value).replace(":", "%3A").replace(",", "%2C")


def _workflow_message(value: str) -> str:
    return value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
