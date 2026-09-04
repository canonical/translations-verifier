"""Safe compact terminal rendering."""

from pathlib import Path

from rich.console import Console
from rich.text import Text

from wlreviser.applying import ApplyResult, ApplyStatus
from wlreviser.models import ItemStatus, VerificationReport


def render_verification(report: VerificationReport, report_path: Path, console: Console) -> None:
    """Render a verification summary without interpreting remote Rich markup."""
    console.print(
        Text(
            f"Verification complete: {report.counts.passed} passed, "
            f"{report.counts.rejected} rejected, {report.counts.errors} errors"
        )
    )
    console.print(Text(f"Report: {report_path}"))

    rejected = [item for item in report.items if item.status is ItemStatus.REJECTED]
    if rejected:
        console.print(Text("\nRejected findings"), style="bold")
    for item in rejected:
        locale = item.weblate_locale or item.filename_locale
        console.print(Text(f"\n{item.id}  {item.component}  {locale}  {item.path}"), style="bold")
        console.print(Text(f"Key: {item.identity.key}"))
        if item.format == "html":
            console.print(Text(f"Reason: {item.reason or ''}"))
            console.print(
                Text("Full HTML and the proposed correction are available in the JSON report.")
            )
            continue
        current = "\n".join(item.pr_target or ())
        proposed = item.suggested_translation or ""
        console.print(Text(f"Current: {current}"))
        console.print(Text(f"Proposed: {proposed}"))
        console.print(Text(f"Reason: {item.reason or ''}"))
        if item.error:
            console.print(Text(f"Mapping error: {item.error}"))

    errors = [item for item in report.items if item.status is ItemStatus.ERROR]
    if errors:
        console.print(Text("\nErrors"), style="bold")
        for item in errors:
            console.print(Text(f"{item.id}  {item.path}: {item.error or 'operation failed'}"))
    if report.warnings:
        console.print(Text("\nWarnings"), style="bold")
        for warning in report.warnings:
            console.print(Text(warning))


def render_apply(result: ApplyResult, console: Console) -> None:
    """Render aggregate apply and component repository outcomes."""
    console.print(
        Text(
            f"Apply complete: {result.updated} updated, "
            f"{result.failed} failed, {result.skipped} skipped"
        )
    )
    for item in result.items:
        if item.status is not ApplyStatus.UPDATED:
            console.print(
                Text(f"{item.id}  {item.status.value}: {item.message or 'operation failed'}")
            )
    for component in result.components:
        commit = component.commit_error or "ok"
        push = (
            "skipped (configured)"
            if component.push_skipped
            else component.push_error or "ok"
        )
        console.print(Text(f"{component.component}  commit: {commit}; push: {push}"))