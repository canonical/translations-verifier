from __future__ import annotations

import base64
import re

from translations_verifier.config import DeterministicPolicy
from translations_verifier.models import Finding, Severity, TranslationOperation

_STRONG_PATTERNS = (
    re.compile(r"(?i)\b(ignore|disregard|override)\b.{0,40}\b(previous|prior|system|developer)\b"),
    re.compile(r"(?i)<\|(?:system|assistant|developer|tool)\|>"),
    re.compile(r"(?i)\b(?:system|assistant|developer)\s*(?:message|prompt)\s*:"),
    re.compile(r'(?i)"(?:tool_calls|function_call|function)"\s*:'),
    re.compile(r"(?i)\bdo not translate\b.{0,40}\b(instruction|prompt|text)\b"),
)
_ENCODED_MARKER = re.compile(r"(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{48,}={0,2}(?![A-Za-z0-9+/])")


def check_prompt_injection(
    operation: TranslationOperation,
    policy: DeterministicPolicy,
) -> list[Finding]:
    del policy
    assert operation.after is not None
    target = operation.after.value
    if any(pattern.search(target) for pattern in _STRONG_PATTERNS) or _contains_encoded_instruction(
        target
    ):
        return [
            Finding(
                rule_id="security.prompt_injection",
                severity=Severity.ERROR,
                message="target contains a strong prompt-injection indicator",
                path=operation.path,
                key=operation.key,
                line=operation.after.line,
            )
        ]
    return []


def _contains_encoded_instruction(value: str) -> bool:
    for match in _ENCODED_MARKER.finditer(value):
        try:
            decoded = base64.b64decode(match.group(0), validate=True).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            continue
        if any(pattern.search(decoded) for pattern in _STRONG_PATTERNS):
            return True
    return False
