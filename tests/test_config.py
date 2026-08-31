from pathlib import Path

import pytest

from translations_verifier.config import ConfigurationError, load_project_config


def test_load_project_config_matches_locale(tmp_path: Path) -> None:
    path = tmp_path / "project.yaml"
    path.write_text(
        """
repository: canonical/example
translation_sets:
  - name: installer
    directory: packages/installer/l10n
    filename_pattern: app_{locale}.arb
    source_locale: en
""",
        encoding="utf-8",
    )

    config = load_project_config(path)

    translation_set = config.translation_sets[0]
    assert translation_set.locale_from_path("packages/installer/l10n/app_de.arb") == "de"
    assert translation_set.path_for_locale("en") == "packages/installer/l10n/app_en.arb"
    assert translation_set.locale_from_path("packages/other/l10n/app_de.arb") is None


def test_project_config_rejects_unknown_fields(tmp_path: Path) -> None:
    path = tmp_path / "project.yaml"
    path.write_text(
        """
repository: canonical/example
unexpected: true
translation_sets:
  - name: installer
    directory: l10n
    filename_pattern: app_{locale}.arb
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError, match="unexpected"):
        load_project_config(path)
