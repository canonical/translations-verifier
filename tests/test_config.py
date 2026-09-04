from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest
from pydantic import ValidationError

from wlreviser.config import ConfigError, ProjectConfig, load_config

type ConfigData = dict[str, object]
type ConfigMutation = Callable[[ConfigData], None]


def config_data() -> ConfigData:
    return {
        "weblate": {
            "api_url": "https://hosted.weblate.org/api",
            "project": "ubuntu-desktop-translations",
        },
        "github": {"repository": "canonical/ubuntu-desktop-provision"},
        "ai": {"api_url": "http://localhost:11434/v1", "model": "qwen3"},
        "components": [
            {
                "weblate_component": "provision-common",
                "source_locale": "en",
                "context_locales": ["de", "fr"],
                "format": "arb",
                "source_file": "packages/provision/lib/l10n/provision_en.arb",
                "path": "packages/provision/lib/l10n/provision_{locale}.arb",
            }
        ],
    }


def add_github_token(data: ConfigData) -> None:
    github = cast(dict[str, object], data["github"])
    github["token"] = "secret"


def add_weblate_url_credentials(data: ConfigData) -> None:
    weblate = cast(dict[str, object], data["weblate"])
    weblate["api_url"] = "https://token@hosted.weblate.org/api"


def remove_locale_placeholder(data: ConfigData) -> None:
    components = cast(list[dict[str, object]], data["components"])
    components[0]["path"] = "packages/provision/lib/l10n/provision.arb"


def duplicate_component(data: ConfigData) -> None:
    components = cast(list[dict[str, object]], data["components"])
    components.append(components[0].copy())


def test_config_resolves_owned_path_and_defaults() -> None:
    config = ProjectConfig.model_validate(config_data())

    match = config.component_for_path("packages/provision/lib/l10n/provision_zh_TW.arb")

    assert config.ai.max_concurrency == 4
    assert config.weblate.api_url == "https://hosted.weblate.org/api/"
    assert match is not None
    assert match[0].weblate_component == "provision-common"
    assert match[0].push_after_commit is True
    assert match[1] == "zh_TW"


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (add_github_token, "Extra inputs"),
        (add_weblate_url_credentials, "must not contain credentials"),
        (remove_locale_placeholder, "exactly one {locale}"),
        (duplicate_component, "weblate_component values must be unique"),
    ],
)
def test_config_rejects_unsafe_or_ambiguous_values(
    mutate: ConfigMutation,
    message: str,
) -> None:
    data = config_data()
    mutate(data)

    with pytest.raises(ValidationError, match=message):
        ProjectConfig.model_validate(data)


def test_load_config_rejects_non_mapping_yaml(tmp_path: Path) -> None:
    path = tmp_path / "wlreviser.yaml"
    path.write_text("- not\n- a\n- mapping\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="invalid configuration"):
        load_config(path)