"""Canonical translation and report contracts."""

import re
import secrets
from collections.abc import Collection
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

from wlreviser.config import TranslationFormat

REPORT_SCHEMA_VERSION: Final = 2
_ITEM_ID_PATTERN = re.compile(r"^WSN-[0-9A-F]{12}$")
NonEmptyText = Annotated[str, Field(min_length=1)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class TranslationIdentity(StrictModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    key: NonEmptyText
    context: str | None = None

    def __hash__(self) -> int:
        return hash((self.key, self.context))


class TranslationUnit(StrictModel):
    identity: TranslationIdentity
    forms: tuple[str, ...] = Field(min_length=1)
    metadata: dict[str, JsonValue] = Field(default_factory=dict)


class ParseIssue(StrictModel):
    key: str | None = None
    message: NonEmptyText


def _empty_units() -> dict[TranslationIdentity, TranslationUnit]:
    return {}


class ParsedCatalog(StrictModel):
    units: dict[TranslationIdentity, TranslationUnit] = Field(default_factory=_empty_units)
    issues: tuple[ParseIssue, ...] = ()


class ChangeKind(StrEnum):
    ADDED = "added"
    REMOVED = "removed"
    UPDATED = "updated"


class TranslationChange(StrictModel):
    kind: ChangeKind
    identity: TranslationIdentity
    old: TranslationUnit | None = None
    new: TranslationUnit | None = None

    @field_validator("kind", mode="before")
    @classmethod
    def decode_kind(cls, value: object) -> object:
        return ChangeKind(value) if isinstance(value, str) else value

    @model_validator(mode="after")
    def validate_sides(self) -> Self:
        expected = {
            ChangeKind.ADDED: (False, True),
            ChangeKind.REMOVED: (True, False),
            ChangeKind.UPDATED: (True, True),
        }[self.kind]
        if (self.old is not None, self.new is not None) != expected:
            raise ValueError(f"{self.kind.value} change has invalid old/new values")
        return self


class ReviewVerdict(StrEnum):
    OK = "ok"
    REJECT = "reject"


class AIReview(StrictModel):
    verdict: ReviewVerdict
    reason: str | None
    suggested_translation: str | None

    @field_validator("verdict", mode="before")
    @classmethod
    def decode_verdict(cls, value: object) -> object:
        return ReviewVerdict(value) if isinstance(value, str) else value

    @model_validator(mode="after")
    def validate_verdict(self) -> Self:
        if self.verdict is ReviewVerdict.OK:
            if self.reason is not None or self.suggested_translation is not None:
                raise ValueError("ok reviews must not include a reason or suggestion")
            return self
        if self.reason is None or not self.reason.strip():
            raise ValueError("reject reviews require a reason")
        if self.suggested_translation is None or not self.suggested_translation:
            raise ValueError("reject reviews require a non-empty suggestion")
        return self


class ItemStatus(StrEnum):
    PASSED = "passed"
    REJECTED = "rejected"
    ERROR = "error"


class PeerTranslation(StrictModel):
    locale: NonEmptyText
    forms: tuple[str, ...] = Field(min_length=1)


class ReviewRequest(StrictModel):
    format: TranslationFormat
    target_locale: NonEmptyText
    source: tuple[str, ...] = Field(min_length=1)
    source_context: str | None = None
    source_metadata: dict[str, JsonValue] = Field(default_factory=dict)
    previous_translation: tuple[str, ...] | None = None
    proposed_translation: tuple[str, ...] = Field(min_length=1)
    peer_context: tuple[PeerTranslation, ...] = ()


class VerificationItem(StrictModel):
    id: str
    status: ItemStatus
    change: ChangeKind
    component: NonEmptyText
    format: TranslationFormat
    path: NonEmptyText
    filename_locale: NonEmptyText
    weblate_locale: str | None = None
    weblate_locale_name: str | None = None
    identity: TranslationIdentity
    source: tuple[str, ...] | None = None
    source_metadata: dict[str, JsonValue] = Field(default_factory=dict)
    old_target: tuple[str, ...] | None = None
    pr_target: tuple[str, ...] | None = None
    target_metadata: dict[str, JsonValue] = Field(default_factory=dict)
    peer_context: tuple[PeerTranslation, ...] = ()
    reason: str | None = None
    suggested_translation: str | None = None
    error: str | None = None
    applyable: bool = False
    translation_url: str | None = None
    unit_id: int | None = Field(default=None, gt=0)
    unit_url: str | None = None

    @field_validator("status", mode="before")
    @classmethod
    def decode_status(cls, value: object) -> object:
        return ItemStatus(value) if isinstance(value, str) else value

    @field_validator("change", mode="before")
    @classmethod
    def decode_change(cls, value: object) -> object:
        return ChangeKind(value) if isinstance(value, str) else value

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        if not _ITEM_ID_PATTERN.fullmatch(value):
            raise ValueError("item ID must have the form WSN-<12 uppercase hex characters>")
        return value

    @model_validator(mode="after")
    def validate_outcome(self) -> Self:
        if self.status is ItemStatus.PASSED:
            if any(
                value is not None
                for value in (self.reason, self.suggested_translation, self.error)
            ):
                raise ValueError("passed items cannot include rejection or error details")
            if self.applyable:
                raise ValueError("passed items cannot be applyable")
        elif self.status is ItemStatus.ERROR:
            if self.error is None or not self.error.strip():
                raise ValueError("error items require an error message")
            if self.applyable:
                raise ValueError("error items cannot be applyable")
        else:
            if self.reason is None or not self.reason.strip() or not self.suggested_translation:
                raise ValueError("rejected items require a reason and suggestion")
            if self.pr_target is None:
                raise ValueError("rejected items require a proposed target")
        if self.applyable and (
            self.format == "html"
            or self.unit_id is None
            or self.pr_target is None
            or len(self.pr_target) != 1
        ):
            raise ValueError(
                "applyable items require a single-form non-HTML target and numeric unit ID"
            )
        return self


class ReportCounts(StrictModel):
    passed: int = Field(ge=0)
    rejected: int = Field(ge=0)
    errors: int = Field(ge=0)

    @property
    def total(self) -> int:
        return self.passed + self.rejected + self.errors


class ReportWeblate(StrictModel):
    api_url: NonEmptyText
    project: NonEmptyText


class VerificationReport(StrictModel):
    schema_version: Literal[2] = REPORT_SCHEMA_VERSION
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    repository: NonEmptyText
    pull_request_url: NonEmptyText
    pull_request_number: int = Field(gt=0)
    base_sha: NonEmptyText
    head_sha: NonEmptyText
    weblate: ReportWeblate
    items: tuple[VerificationItem, ...]
    counts: ReportCounts
    warnings: tuple[str, ...] = ()

    @field_validator("created_at")
    @classmethod
    def validate_created_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("created_at must include a timezone")
        return value

    @model_validator(mode="after")
    def validate_aggregate(self) -> Self:
        ids = [item.id for item in self.items]
        if len(ids) != len(set(ids)):
            raise ValueError("report item IDs must be unique")
        expected = counts_for(self.items)
        if self.counts != expected:
            raise ValueError("report counts do not match report items")
        return self


def new_item_id(existing: Collection[str] = ()) -> str:
    """Generate a readable random item ID without a report-local collision."""
    occupied = set(existing)
    while True:
        candidate = f"WSN-{secrets.token_hex(6).upper()}"
        if candidate not in occupied:
            return candidate


def counts_for(items: Collection[VerificationItem]) -> ReportCounts:
    """Build aggregate report counts from item outcomes."""
    return ReportCounts(
        passed=sum(item.status is ItemStatus.PASSED for item in items),
        rejected=sum(item.status is ItemStatus.REJECTED for item in items),
        errors=sum(item.status is ItemStatus.ERROR for item in items),
    )