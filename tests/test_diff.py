from translations_verifier.arb import parse_arb
from translations_verifier.diff import diff_translations
from translations_verifier.models import OperationType


def test_diff_classifies_target_changes_against_base_target() -> None:
    source = parse_arb(b'{"changed":"Changed","deleted":"Deleted"}', "app_en.arb")
    before = parse_arb(b'{"changed":"Alt","deleted":"Weg"}', "app_de.arb")
    after = parse_arb(b'{"changed":"Neu","inserted":"Hinzu"}', "app_de.arb")

    operations = diff_translations(source=source, before=before, after=after, locale="de")

    assert [(item.operation, item.key) for item in operations] == [
        (OperationType.CHANGED, "changed"),
        (OperationType.DELETED, "deleted"),
        (OperationType.INSERTED, "inserted"),
    ]
    assert operations[1].source is not None
    assert operations[2].source is None


def test_diff_ignores_line_number_changes() -> None:
    source = parse_arb(b'{"first":"First","stable":"Stable"}', "app_en.arb")
    before = parse_arb(b'{"stable":"Stabil"}', "app_de.arb")
    after = parse_arb(b'{\n  "added": "Neu",\n  "stable":"Stabil"\n}', "app_de.arb")

    operations = diff_translations(source=source, before=before, after=after, locale="de")

    assert [(item.operation, item.key) for item in operations] == [
        (OperationType.INSERTED, "added")
    ]
