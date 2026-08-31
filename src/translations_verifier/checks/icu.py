from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from pyicumessageformat import Parser  # type: ignore[import-untyped]

from translations_verifier.config import DeterministicPolicy
from translations_verifier.models import Finding, Severity, TranslationOperation

_PARSER = Parser({"allow_tags": False, "require_other": True})


def check_icu(
    operation: TranslationOperation,
    policy: DeterministicPolicy,
) -> list[Finding]:
    del policy
    assert operation.source is not None
    assert operation.after is not None

    source_tree, source_error = _parse(operation.source.value)
    target_tree, target_error = _parse(operation.after.value)
    if source_error:
        return [
            _finding(
                operation, "icu.invalid_source", f"English ICU syntax is invalid: {source_error}"
            )
        ]
    if target_error:
        return [
            _finding(
                operation, "icu.invalid_target", f"target ICU syntax is invalid: {target_error}"
            )
        ]

    source_signature = _signature(source_tree)
    target_signature = _signature(target_tree)
    findings: list[Finding] = []
    if source_signature.occurrences != target_signature.occurrences:
        findings.append(
            _finding(
                operation,
                "icu.placeholder_mismatch",
                "ICU placeholder names, types, or multiplicities differ from English",
                {
                    "source": _counter_dict(source_signature.occurrences),
                    "target": _counter_dict(target_signature.occurrences),
                },
            )
        )

    for name in sorted(set(source_signature.selectors) | set(target_signature.selectors)):
        source_selectors = source_signature.selectors.get(name, [])
        target_selectors = target_signature.selectors.get(name, [])
        if len(source_selectors) != len(target_selectors):
            findings.append(
                _finding(
                    operation, "icu.selector_mismatch", f"selector structure differs for {name!r}"
                )
            )
            continue
        for source_type, source_options, target_entry in zip(
            source_signature.selector_types.get(name, []),
            source_selectors,
            zip(
                target_signature.selector_types.get(name, []),
                target_selectors,
                strict=False,
            ),
            strict=False,
        ):
            target_type, target_options = target_entry
            if source_type != target_type or not _compatible_options(
                source_type, source_options, target_options
            ):
                findings.append(
                    _finding(
                        operation,
                        "icu.selector_mismatch",
                        f"selector options differ for {name!r}",
                        {"source": sorted(source_options), "target": sorted(target_options)},
                    )
                )
                break

    declared = operation.source.metadata.get("placeholders")
    if isinstance(declared, dict):
        used_names = {name for name, _ in source_signature.occurrences}
        declared_names = set(declared)
        if used_names != declared_names:
            findings.append(
                _finding(
                    operation,
                    "arb.placeholder_metadata_mismatch",
                    "English placeholder metadata does not match placeholders used by the message",
                    {"used": sorted(used_names), "declared": sorted(declared_names)},
                )
            )
    return findings


class _IcuSignature:
    def __init__(self) -> None:
        self.occurrences: Counter[tuple[str, str]] = Counter()
        self.selectors: dict[str, list[set[str]]] = defaultdict(list)
        self.selector_types: dict[str, list[str]] = defaultdict(list)


def _parse(value: str) -> tuple[list[Any], str | None]:
    try:
        return _PARSER.parse(value), None
    except (SyntaxError, ValueError) as exc:
        return [], str(exc)


def _signature(tree: list[Any]) -> _IcuSignature:
    result = _IcuSignature()
    _visit(tree, result)
    return result


def _visit(nodes: list[Any], result: _IcuSignature) -> None:
    for node in nodes:
        if not isinstance(node, dict) or "name" not in node:
            continue
        name = str(node["name"])
        node_type = str(node.get("type", "argument"))
        if node.get("hash"):
            continue
        result.occurrences[(name, node_type)] += 1
        options = node.get("options")
        if isinstance(options, dict):
            result.selectors[name].append(set(options))
            result.selector_types[name].append(node_type)
            for submessage in options.values():
                if isinstance(submessage, list):
                    _visit(submessage, result)


def _compatible_options(kind: str, source: set[str], target: set[str]) -> bool:
    if "other" not in target:
        return False
    if kind == "select":
        return source == target
    source_exact = {option for option in source if option.startswith("=")}
    target_exact = {option for option in target if option.startswith("=")}
    return source_exact == target_exact


def _counter_dict(counter: Counter[tuple[str, str]]) -> dict[str, int]:
    return {f"{name}:{kind}": count for (name, kind), count in sorted(counter.items())}


def _finding(
    operation: TranslationOperation,
    rule_id: str,
    message: str,
    evidence: dict[str, Any] | None = None,
) -> Finding:
    assert operation.after is not None
    return Finding(
        rule_id=rule_id,
        severity=Severity.ERROR,
        message=message,
        path=operation.path,
        key=operation.key,
        line=operation.after.line,
        evidence=evidence or {},
    )
