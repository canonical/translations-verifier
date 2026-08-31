from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OperationType(StrEnum):
    INSERTED = "inserted"
    CHANGED = "changed"
    DELETED = "deleted"


class Severity(StrEnum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


class OverallStatus(StrEnum):
    ACCEPTABLE = "acceptable"
    UNACCEPTABLE = "unacceptable"
    INCOMPLETE = "incomplete"


class PullRequestInfo(StrictModel):
    repository: str
    number: int
    base_ref: str
    base_sha: str
    head_sha: str


class ChangedFile(StrictModel):
    path: str
    status: str
    previous_path: str | None = None
    additions: int = 0
    deletions: int = 0
    changes: int = 0


class ArbMessage(StrictModel):
    key: str
    value: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    line: int | None = None


class ArbDocument(StrictModel):
    path: str
    messages: dict[str, ArbMessage]
    global_metadata: dict[str, Any] = Field(default_factory=dict)


class TranslationOperation(StrictModel):
    operation: OperationType
    key: str
    path: str
    locale: str
    source: ArbMessage | None
    before: ArbMessage | None
    after: ArbMessage | None


class Finding(StrictModel):
    rule_id: str
    severity: Severity
    message: str
    path: str
    key: str | None = None
    line: int | None = None
    evidence: dict[str, Any] = Field(default_factory=dict)


class AiUsage(StrictModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class AiCategory(StrEnum):
    MEANING = "meaning"
    GRAMMAR = "grammar"
    TONE = "tone"
    POLICY = "policy"
    SECURITY = "security"
    ABUSIVE = "abusive"
    OTHER = "other"


class AiVerdict(StrictModel):
    key: str
    acceptable: bool
    categories: list[AiCategory]
    rationale: str = Field(min_length=1, max_length=1000)
    confidence: float = Field(ge=0, le=1)
    human_review_note: str | None = Field(default=None, max_length=1000)
    suggested_translation: str | None = Field(max_length=4000)

    @model_validator(mode="after")
    def validate_suggested_translation(self) -> AiVerdict:
        if self.acceptable and self.suggested_translation is not None:
            raise ValueError("acceptable verdicts must not include a suggested translation")
        if not self.acceptable and (
            self.suggested_translation is None or not self.suggested_translation.strip()
        ):
            raise ValueError("unacceptable verdicts must include a suggested translation")
        return self


class AiReviewState(StrEnum):
    REVIEWED = "reviewed"
    SKIPPED = "skipped"
    FAILED = "failed"


class AiReviewRecord(StrictModel):
    key: str
    path: str
    state: AiReviewState
    verdict: AiVerdict | None = None
    reason: str | None = None
    usage: AiUsage = Field(default_factory=AiUsage)


class VerificationReport(StrictModel):
    schema_version: str = "1.1"
    repository: str
    pull_request: int
    base_sha: str
    head_sha: str
    status: OverallStatus
    project_config_fingerprint: str
    ai_config_fingerprint: str | None = None
    operations: list[TranslationOperation] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)
    ai_reviews: list[AiReviewRecord] = Field(default_factory=list)
    ai_usage: AiUsage = Field(default_factory=AiUsage)
    incomplete_reasons: list[str] = Field(default_factory=list)
    ignored_file_count: int = 0
