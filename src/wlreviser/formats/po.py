"""Monolingual GNU gettext PO adapter."""

from typing import Any, Literal

import polib

from wlreviser.models import ParsedCatalog, ParseIssue, TranslationIdentity, TranslationUnit

from .base import FormatParseError


class PoAdapter:
    name: Literal["po"] = "po"

    def parse(self, content: str, path: str) -> ParsedCatalog:
        del path
        try:
            catalog = polib.pofile(content, wrapwidth=0)
        except (OSError, UnicodeError, ValueError) as exc:
            raise FormatParseError(f"invalid PO document: {exc}") from exc

        units: dict[TranslationIdentity, TranslationUnit] = {}
        issues: list[ParseIssue] = []
        for entry in catalog:
            if entry.obsolete or not entry.msgid:
                continue
            identity = TranslationIdentity(key=entry.msgid, context=entry.msgctxt or None)
            if identity in units:
                issues.append(ParseIssue(key=entry.msgid, message="duplicate PO unit identity"))
                continue
            forms = self._forms(entry)
            metadata: dict[str, Any] = {
                "comment": entry.comment,
                "translator_comment": entry.tcomment,
                "occurrences": [
                    {"file": filename, "line": line_number}
                    for filename, line_number in entry.occurrences
                ],
                "flags": list(entry.flags),
                "fuzzy": entry.fuzzy,
                "plural_key": entry.msgid_plural or None,
                "previous_context": entry.previous_msgctxt or None,
                "previous_key": entry.previous_msgid or None,
                "previous_plural_key": entry.previous_msgid_plural or None,
            }
            units[identity] = TranslationUnit(
                identity=identity,
                forms=forms,
                metadata=metadata,
            )
        return ParsedCatalog(units=units, issues=tuple(issues))

    @staticmethod
    def _forms(entry: Any) -> tuple[str, ...]:
        if entry.msgid_plural and entry.msgstr_plural:
            return tuple(entry.msgstr_plural[index] for index in sorted(entry.msgstr_plural))
        return (entry.msgstr,)