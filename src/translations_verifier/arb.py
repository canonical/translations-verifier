from __future__ import annotations

import json
import re
from collections.abc import Iterable
from typing import Any

from translations_verifier.models import ArbDocument, ArbMessage


class ArbParseError(ValueError):
    def __init__(self, rule_id: str, message: str, line: int | None = None) -> None:
        super().__init__(message)
        self.rule_id = rule_id
        self.line = line


class _DuplicateKeyError(ValueError):
    pass


def parse_arb(content: bytes, path: str) -> ArbDocument:
    try:
        text = content.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ArbParseError("arb.invalid_utf8", f"ARB file is not valid UTF-8: {exc}") from exc

    try:
        raw = json.loads(text, object_pairs_hook=_object_without_duplicates)
    except _DuplicateKeyError as exc:
        raise ArbParseError("arb.duplicate_key", str(exc)) from exc
    except json.JSONDecodeError as exc:
        raise ArbParseError("arb.invalid_json", exc.msg, exc.lineno) from exc

    if not isinstance(raw, dict):
        raise ArbParseError("arb.invalid_root", "ARB root must be a JSON object")

    lines = _key_lines(text)
    metadata: dict[str, dict[str, Any]] = {}
    global_metadata: dict[str, Any] = {}
    messages: dict[str, ArbMessage] = {}

    for key, value in raw.items():
        if key.startswith("@@"):
            global_metadata[key] = value
            continue
        if key.startswith("@"):
            message_key = key[1:]
            if not message_key or not isinstance(value, dict):
                raise ArbParseError(
                    "arb.invalid_metadata",
                    f"metadata {key!r} must be an object associated with a message",
                    lines.get(key),
                )
            _validate_message_metadata(key, value, lines.get(key))
            metadata[message_key] = value
            continue
        if not isinstance(value, str):
            raise ArbParseError(
                "arb.invalid_message",
                f"message {key!r} must have a string value",
                lines.get(key),
            )
        messages[key] = ArbMessage(key=key, value=value, line=lines.get(key))

    orphaned = sorted(set(metadata) - set(messages))
    if orphaned:
        key = orphaned[0]
        raise ArbParseError(
            "arb.orphan_metadata",
            f"metadata exists for missing message {key!r}",
            lines.get(f"@{key}"),
        )

    for key, message_metadata in metadata.items():
        messages[key] = messages[key].model_copy(update={"metadata": message_metadata})

    return ArbDocument(path=path, messages=messages, global_metadata=global_metadata)


def _object_without_duplicates(pairs: Iterable[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKeyError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _key_lines(text: str) -> dict[str, int]:
    result: dict[str, int] = {}
    key_pattern = re.compile(r'^\s*"((?:[^"\\]|\\.)+)"\s*:', re.MULTILINE)
    for match in key_pattern.finditer(text):
        try:
            key = json.loads(f'"{match.group(1)}"')
        except json.JSONDecodeError:
            continue
        result.setdefault(key, text.count("\n", 0, match.start()) + 1)
    return result


def _validate_message_metadata(key: str, metadata: dict[str, Any], line: int | None) -> None:
    description = metadata.get("description")
    if description is not None and not isinstance(description, str):
        raise ArbParseError(
            "arb.invalid_metadata",
            f"metadata {key!r}.description must be a string",
            line,
        )
    placeholders = metadata.get("placeholders")
    if placeholders is None:
        return
    if not isinstance(placeholders, dict):
        raise ArbParseError(
            "arb.invalid_placeholders",
            f"metadata {key!r}.placeholders must be an object",
            line,
        )
    for name, declaration in placeholders.items():
        if not isinstance(name, str) or not isinstance(declaration, dict):
            raise ArbParseError(
                "arb.invalid_placeholders",
                f"placeholder declarations in {key!r} must be named objects",
                line,
            )
        for field_name in ("type", "format", "example"):
            field_value = declaration.get(field_name)
            if field_value is not None and not isinstance(field_value, (str, int, float, bool)):
                raise ArbParseError(
                    "arb.invalid_placeholders",
                    f"placeholder {name!r} field {field_name!r} must be scalar",
                    line,
                )
