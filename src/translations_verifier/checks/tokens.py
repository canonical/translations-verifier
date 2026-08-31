from __future__ import annotations

import re
from collections import Counter
from typing import Any

from translations_verifier.config import DeterministicPolicy
from translations_verifier.models import Finding, Severity, TranslationOperation

_PRINTF = re.compile(
    r"%(?:\([^)]+\))?(?:\d+\$)?[-+#0 ']*(?:\d+|\*)?(?:\.(?:\d+|\*))?"
    r"(?:hh|h|ll|l|j|z|t|L)?[diouxXeEfFgGaAcspn%]"
)
_URL = re.compile(r"https?://[^\s<>\"']+")
_FLAG = re.compile(r"(?<![\w-])--?[A-Za-z][A-Za-z0-9-]*(?:=[^\s]+)?")


def check_tokens(
    operation: TranslationOperation,
    policy: DeterministicPolicy,
) -> list[Finding]:
    assert operation.source is not None
    assert operation.after is not None
    source = operation.source.value
    target = operation.after.value
    findings: list[Finding] = []

    findings.extend(
        _compare(operation, "token.printf_mismatch", "printf token", _PRINTF, source, target)
    )
    if policy.preserve_urls:
        findings.extend(_compare(operation, "token.url_mismatch", "URL", _URL, source, target))
    if policy.preserve_command_flags:
        findings.extend(
            _compare(operation, "token.flag_mismatch", "command flag", _FLAG, source, target)
        )
    for index, pattern in enumerate(policy.variable_patterns):
        findings.extend(
            _compare(
                operation,
                "token.variable_mismatch",
                f"configured variable pattern {index + 1}",
                re.compile(pattern),
                source,
                target,
            )
        )
    for token in policy.immutable_tokens:
        source_count = source.count(token)
        target_count = target.count(token)
        if source_count != target_count:
            findings.append(
                _finding(
                    operation,
                    "token.immutable_mismatch",
                    f"immutable token {token!r} occurs {source_count} time(s) in English and "
                    f"{target_count} time(s) in the target",
                    {"token": token, "source_count": source_count, "target_count": target_count},
                )
            )
    return findings


def _compare(
    operation: TranslationOperation,
    rule_id: str,
    label: str,
    pattern: re.Pattern[str],
    source: str,
    target: str,
) -> list[Finding]:
    source_tokens = Counter(match.group(0) for match in pattern.finditer(source))
    target_tokens = Counter(match.group(0) for match in pattern.finditer(target))
    if source_tokens == target_tokens:
        return []
    return [
        _finding(
            operation,
            rule_id,
            f"{label} values or multiplicities differ from English",
            {"source": dict(source_tokens), "target": dict(target_tokens)},
        )
    ]


def _finding(
    operation: TranslationOperation,
    rule_id: str,
    message: str,
    evidence: dict[str, Any],
) -> Finding:
    assert operation.after is not None
    return Finding(
        rule_id=rule_id,
        severity=Severity.ERROR,
        message=message,
        path=operation.path,
        key=operation.key,
        line=operation.after.line,
        evidence=evidence,
    )
