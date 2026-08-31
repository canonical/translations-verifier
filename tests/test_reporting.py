from translations_verifier.models import (
    Finding,
    OverallStatus,
    Severity,
    VerificationReport,
)
from translations_verifier.reporting import render_github_annotations, render_markdown


def _report() -> VerificationReport:
    return VerificationReport(
        repository="canonical/example",
        pull_request=42,
        base_sha="base",
        head_sha="head",
        status=OverallStatus.UNACCEPTABLE,
        project_config_fingerprint="fingerprint",
        findings=[
            Finding(
                rule_id="text.bad",
                severity=Severity.ERROR,
                message="line one\n::warning::injected | table",
                path="l10n/app_de.arb",
                key="hello",
                line=7,
                evidence={"suggested_translation": "Richtige | Uebersetzung\nmit Zeile"},
            )
        ],
    )


def test_markdown_escapes_untrusted_structure() -> None:
    rendered = render_markdown(_report())

    assert "line one ::warning::injected \\| table" in rendered
    assert "Suggested translation: Richtige \\| Uebersetzung mit Zeile" in rendered
    assert rendered.count("| text.bad |") == 0


def test_annotations_encode_workflow_commands() -> None:
    rendered = render_github_annotations(_report())

    assert rendered.startswith("::error file=l10n/app_de.arb,line=7,title=text.bad::")
    assert "%0A::warning::injected" in rendered
    assert "Suggested translation: Richtige | Uebersetzung%0Amit Zeile" in rendered
    assert rendered.count("\n") == 1
