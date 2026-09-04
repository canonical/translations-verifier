from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from wlreviser.models import (
    AIReview,
    ChangeKind,
    ItemStatus,
    ReportCounts,
    ReportWeblate,
    ReviewVerdict,
    TranslationIdentity,
    VerificationItem,
    VerificationReport,
    counts_for,
    new_item_id,
)


def rejected_item(item_id: str = "WSN-0123456789AB") -> VerificationItem:
    return VerificationItem(
        id=item_id,
        status=ItemStatus.REJECTED,
        change=ChangeKind.UPDATED,
        component="provision-common",
        format="arb",
        path="l10n/app_de.arb",
        filename_locale="de",
        identity=TranslationIdentity(key="welcome"),
        source=("Welcome",),
        old_target=("Willkommen",),
        pr_target=("Willkommen!",),
        reason="The punctuation changes the specified tone.",
        suggested_translation="Willkommen",
    )


def report(
    items: tuple[VerificationItem, ...],
    counts: ReportCounts,
) -> VerificationReport:
    return VerificationReport(
        created_at=datetime.now(UTC),
        repository="canonical/ubuntu-desktop-provision",
        pull_request_url="https://github.com/canonical/ubuntu-desktop-provision/pull/1",
        pull_request_number=1,
        base_sha="a" * 40,
        head_sha="b" * 40,
        weblate=ReportWeblate(api_url="https://hosted.weblate.org/api/", project="p"),
        items=items,
        counts=counts,
    )


def test_ai_review_enforces_semantic_contract() -> None:
    review = AIReview.model_validate(
        {
            "verdict": "reject",
            "reason": "Wrong meaning",
            "suggested_translation": "Korrektur",
        }
    )

    assert review.verdict is ReviewVerdict.REJECT
    assert review.suggested_translation == "Korrektur"

    with pytest.raises(ValidationError, match="valid string"):
        AIReview.model_validate(
            {
                "verdict": "reject",
                "reason": "Wrong meaning",
                "suggested_translation": ["Korrektur"],
            }
        )

    with pytest.raises(ValidationError, match="ok reviews"):
        AIReview(verdict=ReviewVerdict.OK, reason="No", suggested_translation=None)


def test_report_rejects_duplicate_ids_and_incorrect_counts() -> None:
    item = rejected_item()

    with pytest.raises(ValidationError, match="counts do not match"):
        report((item,), ReportCounts(passed=1, rejected=0, errors=0))

    with pytest.raises(ValidationError, match="IDs must be unique"):
        report((item, item), ReportCounts(passed=0, rejected=2, errors=0))


def test_report_rejects_applyable_multi_form_suggestion() -> None:
    data = rejected_item().model_dump()
    data.update(
        {
            "pr_target": ("Eine Datei", "Viele Dateien"),
            "applyable": True,
            "unit_id": 1,
        }
    )

    with pytest.raises(ValidationError, match="single-form"):
        VerificationItem.model_validate(data)


def test_generated_id_and_counts() -> None:
    item = rejected_item()

    assert new_item_id({item.id}).startswith("WSN-")
    assert new_item_id({item.id}) != item.id
    assert counts_for((item,)) == ReportCounts(passed=0, rejected=1, errors=0)