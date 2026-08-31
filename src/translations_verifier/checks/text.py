from __future__ import annotations

import re
import unicodedata

from translations_verifier.config import DeterministicPolicy
from translations_verifier.models import Finding, Severity, TranslationOperation

_ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
_BIDI_DANGEROUS = {"\u202a", "\u202b", "\u202c", "\u202d", "\u202e"}
_BIDI_ISOLATES = {"\u2066", "\u2067", "\u2068", "\u2069"}
_ZERO_WIDTH = {"\u200b", "\u200c", "\u200d", "\ufeff"}


def check_text_safety(
    operation: TranslationOperation,
    policy: DeterministicPolicy,
) -> list[Finding]:
    assert operation.source is not None
    assert operation.after is not None
    source = operation.source.value
    target = operation.after.value
    findings: list[Finding] = []

    if not target.strip():
        findings.append(
            _finding(operation, "text.empty", "target translation is empty or whitespace-only")
        )
    if _ANSI.search(target):
        findings.append(
            _finding(operation, "text.ansi_escape", "target contains an ANSI escape sequence")
        )
    if any(character in _BIDI_DANGEROUS for character in target):
        findings.append(
            _finding(
                operation, "text.bidi_override", "target contains a bidi override or embedding"
            )
        )
    if not policy.allow_bidi_isolates and any(character in _BIDI_ISOLATES for character in target):
        findings.append(
            _finding(operation, "text.bidi_isolate", "target contains a disallowed bidi isolate")
        )
    if any(character in _ZERO_WIDTH for character in target):
        findings.append(
            _finding(operation, "text.zero_width", "target contains a zero-width character")
        )

    unsafe_controls = sorted(
        {f"U+{ord(character):04X}" for character in target if _is_unsafe_control(character)}
    )
    if unsafe_controls:
        findings.append(
            _finding(
                operation,
                "text.control_character",
                f"target contains unsafe control characters: {', '.join(unsafe_controls)}",
            )
        )
    if unicodedata.normalize("NFC", target) != target:
        findings.append(
            _finding(
                operation,
                "text.non_normalized",
                "target is not in Unicode NFC normalization form, actual ("
                + target
                + "); expected ("
                + unicodedata.normalize("NFC", target)
                + ")",
                Severity.WARNING,
            )
        )
    if policy.preserve_whitespace:
        source_edges = (_leading_whitespace(source), _trailing_whitespace(source))
        target_edges = (_leading_whitespace(target), _trailing_whitespace(target))
        if source_edges != target_edges:
            findings.append(
                _finding(
                    operation,
                    "text.edge_whitespace_mismatch",
                    "leading or trailing whitespace differs from English",
                )
            )
        for character, name in (("\n", "newline"), ("\t", "tab"), ("\\", "backslash")):
            if source.count(character) != target.count(character):
                findings.append(
                    _finding(
                        operation,
                        f"text.{name}_mismatch",
                        f"{name} multiplicity differs from English",
                    )
                )
    return findings


def _is_unsafe_control(character: str) -> bool:
    if character in {"\n", "\r", "\t"}:
        return False
    return unicodedata.category(character) in {"Cc", "Cs"}


def _leading_whitespace(value: str) -> str:
    return value[: len(value) - len(value.lstrip())]


def _trailing_whitespace(value: str) -> str:
    return value[len(value.rstrip()) :]


def _finding(
    operation: TranslationOperation,
    rule_id: str,
    message: str,
    severity: Severity = Severity.ERROR,
) -> Finding:
    assert operation.after is not None
    return Finding(
        rule_id=rule_id,
        severity=severity,
        message=message,
        path=operation.path,
        key=operation.key,
        line=operation.after.line,
    )
