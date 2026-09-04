import pytest

from wlreviser.formats import (
    ArbAdapter,
    FormatParseError,
    HtmlAdapter,
    PoAdapter,
    diff_catalogs,
    structural_error,
)
from wlreviser.models import ChangeKind, ParsedCatalog, TranslationIdentity, TranslationUnit


def test_arb_parses_units_metadata_and_reports_bad_values() -> None:
    catalog = ArbAdapter().parse(
        """{
          "@@locale": "en",
          "welcome": "Welcome, {name}!",
          "@welcome": {"description": "Greeting"},
          "count": "{count, plural, one{One} other{{count}}}",
          "broken": 3
        }""",
        "l10n/app_en.arb",
    )

    welcome = catalog.units[TranslationIdentity(key="welcome")]
    assert welcome.forms == ("Welcome, {name}!",)
    assert welcome.metadata == {"description": "Greeting"}
    assert TranslationIdentity(key="@@locale") not in catalog.units
    assert catalog.issues[0].key == "broken"


@pytest.mark.parametrize("content", ["{", "[]", '{"key": "a", "key": "b"}'])
def test_arb_rejects_malformed_documents(content: str) -> None:
    with pytest.raises(FormatParseError):
        ArbAdapter().parse(content, "app_en.arb")


def test_po_parses_monolingual_context_plural_and_metadata() -> None:
    content = '''msgid ""
msgstr "Project-Id-Version: test\\n"

# Translator note
#. Extracted note
#: app.py:10
#, fuzzy, python-format
msgctxt "menu"
msgid "files-key"
msgid_plural "files-key-plural"
msgstr[0] "One file"
msgstr[1] "Many files"

#~ msgid "old"
#~ msgstr "Old"
'''

    catalog = PoAdapter().parse(content, "locale/en.po")
    unit = catalog.units[TranslationIdentity(key="files-key", context="menu")]

    assert unit.forms == ("One file", "Many files")
    assert unit.metadata["fuzzy"] is True
    assert unit.metadata["occurrences"] == [{"file": "app.py", "line": "10"}]
    assert len(catalog.units) == 1


def test_html_is_one_full_document_unit() -> None:
    content = "<!doctype html>\n<h1>Hello</h1>\n"

    catalog = HtmlAdapter().parse(content, "slides/en/welcome.html")
    unit = catalog.units[TranslationIdentity(key="welcome.html")]

    assert unit.forms == (content,)
    assert unit.metadata == {}


def test_diff_ignores_metadata_and_orders_membership_and_value_changes() -> None:
    old = ParsedCatalog(
        units={
            TranslationIdentity(key="removed"): TranslationUnit(
                identity=TranslationIdentity(key="removed"), forms=("old",)
            ),
            TranslationIdentity(key="same"): TranslationUnit(
                identity=TranslationIdentity(key="same"),
                forms=("same",),
                metadata={"description": "old"},
            ),
            TranslationIdentity(key="updated"): TranslationUnit(
                identity=TranslationIdentity(key="updated"), forms=("before",)
            ),
        }
    )
    new = ParsedCatalog(
        units={
            TranslationIdentity(key="added"): TranslationUnit(
                identity=TranslationIdentity(key="added"), forms=("new",)
            ),
            TranslationIdentity(key="same"): TranslationUnit(
                identity=TranslationIdentity(key="same"),
                forms=("same",),
                metadata={"description": "new"},
            ),
            TranslationIdentity(key="updated"): TranslationUnit(
                identity=TranslationIdentity(key="updated"), forms=("after",)
            ),
        }
    )

    changes = diff_catalogs(old, new)

    assert [(change.identity.key, change.kind) for change in changes] == [
        ("added", ChangeKind.ADDED),
        ("removed", ChangeKind.REMOVED),
        ("updated", ChangeKind.UPDATED),
    ]


def test_structural_rules_require_source_membership() -> None:
    identity = TranslationIdentity(key="key")
    unit = TranslationUnit(identity=identity, forms=("text",))
    source = ParsedCatalog(units={identity: unit})
    added = diff_catalogs(ParsedCatalog(), ParsedCatalog(units={identity: unit}))[0]
    removed = diff_catalogs(ParsedCatalog(units={identity: unit}), ParsedCatalog())[0]

    assert structural_error(added, source, format_name="arb") is None
    assert structural_error(removed, source, format_name="arb") is not None
    assert structural_error(removed, ParsedCatalog(), format_name="po") is None
    assert structural_error(removed, source, format_name="html", source_exists=True) is not None