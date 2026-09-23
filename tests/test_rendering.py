from pathlib import Path

from rich.console import Console

from tests.test_applying import report
from wlreviser.applying import ApplyResult, ComponentOperationResult
from wlreviser.models import (
    ChangeKind,
    ItemStatus,
    ReportCounts,
    TranslationIdentity,
    VerificationItem,
)
from wlreviser.rendering import render_apply, render_verification


def test_html_rendering_omits_documents_and_treats_markup_as_text() -> None:
    html_item = VerificationItem(
        id="WL-ABCDEF012345",
        status=ItemStatus.REJECTED,
        change=ChangeKind.UPDATED,
        component="component",
        format="html",
        path="l10n/app_de.arb",
        filename_locale="de",
        identity=TranslationIdentity(key="slide.html"),
        source=("<h1>SOURCE_SENTINEL</h1>",),
        pr_target=("[bold]CURRENT_SENTINEL[/bold]",),
        reason="Unsafe [red]meaning[/red]",
        suggested_translation="<h1>SUGGESTION_SENTINEL</h1>",
    )
    value = report()
    items = (html_item,)
    value = value.model_copy(
        update={
            "items": items,
            "counts": ReportCounts(passed=0, rejected=1, errors=0),
        }
    )
    console = Console(record=True, width=80)

    render_verification(value, Path("report.json"), console)
    output = console.export_text()

    assert "SOURCE_SENTINEL" not in output
    assert "CURRENT_SENTINEL" not in output
    assert "SUGGESTION_SENTINEL" not in output
    assert "Unsafe [red]meaning[/red]" in output
    assert "JSON report" in output


def test_non_html_rendering_includes_source_current_and_proposed_text() -> None:
    item = VerificationItem(
        id="WL-ABCDEF012345",
        status=ItemStatus.REJECTED,
        change=ChangeKind.UPDATED,
        component="component",
        format="po",
        path="po/de.po",
        filename_locale="de",
        identity=TranslationIdentity(key="files"),
        source=("One file", "Many files"),
        pr_target=("Eine Datei", "Viele Dateien"),
        reason="The plural translation is inaccurate.",
        suggested_translation="Eine Datei / Viele Dateien",
    )
    value = report().model_copy(
        update={
            "items": (item,),
            "counts": ReportCounts(passed=0, rejected=1, errors=0),
        }
    )
    console = Console(record=True, width=80)

    render_verification(value, Path("report.json"), console)
    output = console.export_text()

    assert "Source: One file\nMany files" in output
    assert output.index("Source:") < output.index("Current:")
    assert output.index("Current:") < output.index("Proposed:")
    assert "Current: Eine Datei\nViele Dateien" in output
    assert "Proposed: Eine Datei / Viele Dateien" in output
    assert "Reason: The plural translation is inaccurate." in output


def test_apply_rendering_distinguishes_configured_push_skip() -> None:
    result = ApplyResult(
        items=(),
        components=(
            ComponentOperationResult(component="component", push_skipped=True),
        ),
    )
    console = Console(record=True, width=80)

    render_apply(result, console)

    assert "commit: ok; push: skipped (configured)" in console.export_text()