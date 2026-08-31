from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections import Counter
from collections.abc import Sequence
from html.parser import HTMLParser

from translations_verifier.config import DeterministicPolicy, MarkupPolicy
from translations_verifier.models import Finding, Severity, TranslationOperation

_MARKUP = re.compile(r"</?[A-Za-z][^>]*>")
_HTML_VOID_TAGS = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}


def check_markup(
    operation: TranslationOperation,
    policy: DeterministicPolicy,
) -> list[Finding]:
    assert operation.source is not None
    assert operation.after is not None
    source = operation.source.value
    target = operation.after.value
    markup_policy = policy.markup

    if markup_policy.mode == "none":
        if _MARKUP.search(source) or _MARKUP.search(target):
            return [_finding(operation, "markup.disallowed", "markup is disabled by policy")]
        return []

    try:
        source_signature = _parse(source, markup_policy)
    except ValueError as exc:
        return [_finding(operation, "markup.invalid_source", f"English markup is invalid: {exc}")]
    try:
        target_signature = _parse(target, markup_policy)
    except ValueError as exc:
        return [_finding(operation, "markup.invalid_target", f"target markup is invalid: {exc}")]

    findings: list[Finding] = []
    if (
        source_signature.tags != target_signature.tags
        or source_signature.structure != target_signature.structure
    ):
        findings.append(
            _finding(
                operation,
                "markup.structure_mismatch",
                "target tag names or multiplicities differ from English",
            )
        )
    if source_signature.immutable_attributes != target_signature.immutable_attributes:
        findings.append(
            _finding(
                operation,
                "markup.attribute_mismatch",
                "immutable markup attribute values differ from English",
            )
        )
    return findings


class _MarkupSignature:
    def __init__(self) -> None:
        self.tags: Counter[str] = Counter()
        self.structure: list[tuple[str, str]] = []
        self.immutable_attributes: Counter[tuple[str, str, str]] = Counter()


class _StrictHtmlParser(HTMLParser):
    def __init__(self, policy: MarkupPolicy, signature: _MarkupSignature) -> None:
        super().__init__(convert_charrefs=True)
        self.policy = policy
        self.signature = signature
        self.stack: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._record(tag, attrs)
        if tag in _HTML_VOID_TAGS:
            self.signature.structure.append(("empty", tag))
        else:
            self.signature.structure.append(("start", tag))
            self.stack.append(tag)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._record(tag, attrs)
        self.signature.structure.append(("empty", tag))

    def handle_endtag(self, tag: str) -> None:
        if not self.stack or self.stack.pop() != tag:
            raise ValueError(f"mismatched closing tag {tag!r}")
        self.signature.structure.append(("end", tag))

    def close(self) -> None:
        super().close()
        if self.stack:
            raise ValueError(f"unclosed tag {self.stack[-1]!r}")

    def _record(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        _validate_tag(tag, attrs, self.policy)
        self.signature.tags[tag] += 1
        for name, value in attrs:
            if name in self.policy.immutable_attributes:
                self.signature.immutable_attributes[(tag, name, value or "")] += 1


def _parse(value: str, policy: MarkupPolicy) -> _MarkupSignature:
    signature = _MarkupSignature()
    if policy.mode == "html":
        parser = _StrictHtmlParser(policy, signature)
        parser.feed(value)
        parser.close()
        return signature

    try:
        root = ET.fromstring(f"<translation-root>{value}</translation-root>")
    except ET.ParseError as exc:
        raise ValueError(str(exc)) from exc

    def visit(element: ET.Element[str]) -> None:
        if element is not root:
            signature.structure.append(("start", element.tag))
            attrs = [(str(name), str(value)) for name, value in element.attrib.items()]
            _validate_tag(element.tag, attrs, policy)
            signature.tags[element.tag] += 1
            for name, attribute_value in attrs:
                if name in policy.immutable_attributes:
                    signature.immutable_attributes[(element.tag, name, attribute_value)] += 1
        for child in element:
            visit(child)
        if element is not root:
            signature.structure.append(("end", element.tag))

    visit(root)
    return signature


def _validate_tag(tag: str, attrs: Sequence[tuple[str, str | None]], policy: MarkupPolicy) -> None:
    if tag.lower() in {"script", "style", "iframe", "object", "embed"}:
        raise ValueError(f"unsafe tag {tag!r}")
    if policy.allowed_tags and tag not in policy.allowed_tags:
        raise ValueError(f"tag {tag!r} is not allowlisted")
    allowed_attrs = policy.allowed_attributes.get(tag, ())
    for name, value in attrs:
        if name.lower().startswith("on") or name not in allowed_attrs:
            raise ValueError(f"attribute {name!r} is not allowed on tag {tag!r}")
        if name in {"href", "src"} and value and value.strip().lower().startswith("javascript:"):
            raise ValueError(f"unsafe URL in attribute {name!r}")


def _finding(operation: TranslationOperation, rule_id: str, message: str) -> Finding:
    assert operation.after is not None
    return Finding(
        rule_id=rule_id,
        severity=Severity.ERROR,
        message=message,
        path=operation.path,
        key=operation.key,
        line=operation.after.line,
    )
