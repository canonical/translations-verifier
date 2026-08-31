from __future__ import annotations

from collections.abc import Callable

from translations_verifier.checks.icu import check_icu
from translations_verifier.checks.injection import check_prompt_injection
from translations_verifier.checks.markup import check_markup
from translations_verifier.checks.text import check_text_safety
from translations_verifier.checks.tokens import check_tokens
from translations_verifier.config import DeterministicPolicy
from translations_verifier.models import Finding, OperationType, Severity, TranslationOperation

Validator = Callable[[TranslationOperation, DeterministicPolicy], list[Finding]]

VALIDATORS: tuple[Validator, ...] = (
    check_icu,
    check_tokens,
    check_markup,
    check_text_safety,
    check_prompt_injection,
)


def validate_operation(
    operation: TranslationOperation,
    policy: DeterministicPolicy,
) -> list[Finding]:
    if operation.operation is OperationType.DELETED:
        if operation.source is None:
            return []
        return [
            _finding(
                operation,
                "arb.required_key_deleted",
                "target key was deleted while it remains in the English source",
            )
        ]
    if operation.source is None:
        return [
            _finding(
                operation,
                "arb.unknown_source_key",
                "target key does not exist in the English source",
            )
        ]
    if operation.after is None:
        return [_finding(operation, "arb.missing_target", "target message is missing")]

    findings: list[Finding] = []
    for validator in VALIDATORS:
        findings.extend(validator(operation, policy))
    return findings


def has_blocking_findings(findings: list[Finding]) -> bool:
    return any(finding.severity is Severity.ERROR for finding in findings)


def _finding(operation: TranslationOperation, rule_id: str, message: str) -> Finding:
    return Finding(
        rule_id=rule_id,
        severity=Severity.ERROR,
        message=message,
        path=operation.path,
        key=operation.key,
        line=operation.after.line
        if operation.after
        else operation.before.line
        if operation.before
        else None,
    )
