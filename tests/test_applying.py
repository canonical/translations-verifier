from datetime import UTC, datetime

import pytest

from wlreviser.applying import ApplyService, ApplyStatus
from wlreviser.config import ProjectConfig
from wlreviser.errors import ExternalServiceError, PreflightError
from wlreviser.models import (
    ChangeKind,
    ItemStatus,
    ReportWeblate,
    TranslationIdentity,
    VerificationItem,
    VerificationReport,
    counts_for,
)


def config(*, push_after_commit: bool = True) -> ProjectConfig:
    return ProjectConfig.model_validate(
        {
            "weblate": {"api_url": "https://weblate.example/api", "project": "project"},
            "github": {"repository": "canonical/repo"},
            "ai": {"api_url": "http://localhost:11434/v1", "model": "model"},
            "components": [
                {
                    "weblate_component": "component",
                    "source_locale": "en",
                    "context_locales": [],
                    "format": "arb",
                    "source_file": "l10n/app_en.arb",
                    "path": "l10n/app_{locale}.arb",
                    "push_after_commit": push_after_commit,
                }
            ],
        }
    )


def item(
    item_id: str,
    *,
    status: ItemStatus = ItemStatus.REJECTED,
    applyable: bool = True,
    unit_id: int | None = 1,
) -> VerificationItem:
    rejected = status is ItemStatus.REJECTED
    return VerificationItem(
        id=item_id,
        status=status,
        change=ChangeKind.UPDATED,
        component="component",
        format="arb",
        path="l10n/app_de.arb",
        filename_locale="de",
        identity=TranslationIdentity(key=item_id),
        source=("Hello",),
        old_target=("Hallo",),
        pr_target=("Halloh",),
        reason="Typo" if rejected else None,
        suggested_translation="Hallo" if rejected else None,
        applyable=applyable,
        unit_id=unit_id,
    )


def report() -> VerificationReport:
    items = (
        item("WL-000000000001", unit_id=1),
        item("WL-000000000002", unit_id=2),
        item("WL-000000000003", status=ItemStatus.PASSED, applyable=False, unit_id=None),
    )
    return VerificationReport(
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        repository="canonical/repo",
        pull_request_url="https://github.com/canonical/repo/pull/1",
        pull_request_number=1,
        base_sha="a" * 40,
        head_sha="b" * 40,
        weblate=ReportWeblate(api_url="https://weblate.example/api/", project="project"),
        items=items,
        counts=counts_for(items),
    )


class FakeGateway:
    def __init__(self) -> None:
        self.updates: list[tuple[int, tuple[str, ...]]] = []
        self.commits: list[str] = []
        self.pushes: list[str] = []

    def update_unit(self, unit_id: int, target: tuple[str, ...]) -> None:
        if unit_id == 2:
            raise ExternalServiceError("unit update failed")
        self.updates.append((unit_id, target))

    def commit_component(self, component: str) -> None:
        self.commits.append(component)

    def push_component(self, component: str) -> None:
        self.pushes.append(component)


def test_apply_continues_and_operates_only_on_successful_components() -> None:
    gateway = FakeGateway()
    service = ApplyService(config(), gateway)  # type: ignore[arg-type]

    result = service.apply(
        report(),
        [
            "WL-000000000001",
            "WL-000000000001",
            "unknown",
            "WL-000000000003",
            "WL-000000000002",
        ],
    )

    assert result.updated == 1
    assert result.failed == 1
    assert result.skipped == 3
    assert [item.status for item in result.items] == [
        ApplyStatus.UPDATED,
        ApplyStatus.SKIPPED,
        ApplyStatus.SKIPPED,
        ApplyStatus.SKIPPED,
        ApplyStatus.FAILED,
    ]
    assert gateway.updates == [(1, ("Hallo",))]
    assert gateway.commits == ["component"]
    assert gateway.pushes == ["component"]


def test_apply_all_selects_only_applyable_rejections() -> None:
    gateway = FakeGateway()

    result = ApplyService(config(), gateway).apply(report(), ["all"])  # type: ignore[arg-type]

    assert result.updated == 1
    assert result.failed == 1
    assert result.skipped == 0


def test_apply_skips_push_when_component_uses_automatic_push() -> None:
    gateway = FakeGateway()
    service = ApplyService(config(push_after_commit=False), gateway)  # type: ignore[arg-type]

    result = service.apply(report(), ["WL-000000000001"])

    assert gateway.commits == ["component"]
    assert gateway.pushes == []
    assert result.components[0].push_skipped is True
    assert result.components[0].push_error is None


def test_apply_rejects_mixed_all_and_report_mismatch() -> None:
    service = ApplyService(config(), FakeGateway())  # type: ignore[arg-type]

    with pytest.raises(PreflightError, match="cannot be combined"):
        service.apply(report(), ["all", "WL-000000000001"])

    mismatched = report().model_copy(update={"repository": "other/repo"})
    with pytest.raises(PreflightError, match="repository"):
        service.apply(mismatched, ["all"])