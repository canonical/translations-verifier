"""Application Resource Bundle adapter."""

import json
from typing import Any, Literal, cast

from wlreviser.models import (
    JsonValue,
    ParsedCatalog,
    ParseIssue,
    TranslationIdentity,
    TranslationUnit,
)

from .base import FormatParseError


class _DuplicateKeyError(ValueError):
    pass


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKeyError(f"duplicate JSON property {key!r}")
        result[key] = value
    return result


class ArbAdapter:
    name: Literal["arb"] = "arb"

    def parse(self, content: str, path: str) -> ParsedCatalog:
        del path
        try:
            raw_value = cast(
                object,
                json.loads(content, object_pairs_hook=_object_without_duplicates),
            )
        except (json.JSONDecodeError, _DuplicateKeyError) as exc:
            raise FormatParseError(f"invalid ARB JSON: {exc}") from exc
        if not isinstance(raw_value, dict):
            raise FormatParseError("invalid ARB JSON: top-level value must be an object")
        raw = cast(dict[str, JsonValue], raw_value)

        units: dict[TranslationIdentity, TranslationUnit] = {}
        issues: list[ParseIssue] = []
        for key, value in raw.items():
            if key.startswith("@"):
                continue
            if not isinstance(value, str):
                issues.append(ParseIssue(key=key, message="ARB translation value must be a string"))
                continue
            metadata_value = raw.get(f"@{key}", {})
            if not isinstance(metadata_value, dict):
                issues.append(ParseIssue(key=key, message="ARB unit metadata must be an object"))
                metadata_value = {}
            identity = TranslationIdentity(key=key)
            units[identity] = TranslationUnit(
                identity=identity,
                forms=(value,),
                metadata=metadata_value,
            )
        return ParsedCatalog(units=units, issues=tuple(issues))