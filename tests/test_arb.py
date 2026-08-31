import pytest

from translations_verifier.arb import ArbParseError, parse_arb


def test_parse_arb_associates_metadata_and_lines() -> None:
    document = parse_arb(
        b'{\n  "@@locale": "de",\n  "hello": "Hallo {name}",\n'
        b'  "@hello": {"description": "Greeting", "placeholders": {"name": {}}}\n}',
        "l10n/app_de.arb",
    )

    assert document.global_metadata == {"@@locale": "de"}
    assert document.messages["hello"].line == 3
    assert document.messages["hello"].metadata["description"] == "Greeting"


@pytest.mark.parametrize(
    ("content", "rule_id"),
    [
        (b'{"key":"one","key":"two"}', "arb.duplicate_key"),
        (b'["value"]', "arb.invalid_root"),
        (b'{"key": 1}', "arb.invalid_message"),
        (b'{"@key": "metadata"}', "arb.invalid_metadata"),
        (b'{"@missing": {}}', "arb.orphan_metadata"),
        (b"\xff", "arb.invalid_utf8"),
    ],
)
def test_parse_arb_rejects_invalid_input(content: bytes, rule_id: str) -> None:
    with pytest.raises(ArbParseError) as error:
        parse_arb(content, "app_de.arb")

    assert error.value.rule_id == rule_id
