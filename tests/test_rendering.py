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
        id="WSN-ABCDEF012345",
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