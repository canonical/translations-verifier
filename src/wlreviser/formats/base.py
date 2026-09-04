"""Translation adapter protocol, registry, and shared diff behavior."""

from typing import Protocol

from wlreviser.config import TranslationFormat
from wlreviser.models import ChangeKind, ParsedCatalog, TranslationChange


class FormatParseError(ValueError):
    """Raised when a translation document cannot be parsed."""


class FormatAdapter(Protocol):
    @property
    def name(self) -> TranslationFormat: ...

    def parse(self, content: str, path: str) -> ParsedCatalog:
        """Parse one UTF-8 repository document into canonical units."""
        ...


def get_adapter(format_name: TranslationFormat) -> FormatAdapter:
    """Return the adapter registered for a supported format."""
    from wlreviser.formats.arb import ArbAdapter
    from wlreviser.formats.html import HtmlAdapter
    from wlreviser.formats.po import PoAdapter

    adapters: dict[TranslationFormat, FormatAdapter] = {
        "arb": ArbAdapter(),
        "po": PoAdapter(),
        "html": HtmlAdapter(),
    }
    return adapters[format_name]


def diff_catalogs(old: ParsedCatalog, new: ParsedCatalog) -> tuple[TranslationChange, ...]:
    """Compare unit membership and displayed forms while ignoring metadata-only changes."""
    identities = sorted(
        old.units.keys() | new.units.keys(),
        key=lambda identity: (identity.key, identity.context or ""),
    )
    changes: list[TranslationChange] = []
    for identity in identities:
        old_unit = old.units.get(identity)
        new_unit = new.units.get(identity)
        if old_unit is None:
            changes.append(
                TranslationChange(
                    kind=ChangeKind.ADDED,
                    identity=identity,
                    new=new_unit,
                )
            )
        elif new_unit is None:
            changes.append(
                TranslationChange(
                    kind=ChangeKind.REMOVED,
                    identity=identity,
                    old=old_unit,
                )
            )
        elif old_unit.forms != new_unit.forms:
            changes.append(
                TranslationChange(
                    kind=ChangeKind.UPDATED,
                    identity=identity,
                    old=old_unit,
                    new=new_unit,
                )
            )
    return tuple(changes)


def structural_error(
    change: TranslationChange,
    source: ParsedCatalog,
    *,
    format_name: TranslationFormat,
    source_exists: bool = True,
) -> str | None:
    """Return an error when target membership conflicts with the base source document."""
    if change.kind is ChangeKind.UPDATED:
        return None
    if format_name == "html":
        if change.kind is ChangeKind.ADDED and not source_exists:
            return "target document was added without a source document"
        if change.kind is ChangeKind.REMOVED and source_exists:
            return "target document was removed while its source document still exists"
        return None
    source_contains_identity = change.identity in source.units
    if change.kind is ChangeKind.ADDED and not source_contains_identity:
        return "target unit was added but does not exist in the base source file"
    if change.kind is ChangeKind.REMOVED and source_contains_identity:
        return "target unit was removed while it still exists in the base source file"
    return None