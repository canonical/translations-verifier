import json
import tomllib
from pathlib import Path
from typing import Any

import wlreviser

ROOT = Path(__file__).parents[1]


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def load_toml(path: Path) -> dict[str, Any]:
    return tomllib.loads(path.read_text())


def test_version_files_are_synchronized() -> None:
    project_version = load_toml(ROOT / "pyproject.toml")["project"]["version"]
    lock_packages = [
        package
        for package in load_toml(ROOT / "uv.lock")["package"]
        if package["name"] == "wlreviser" and package.get("source") == {"editable": "."}
    ]

    assert len(lock_packages) == 1
    assert project_version == wlreviser.__version__ == lock_packages[0]["version"]

    released_version = load_json(ROOT / ".release-please-manifest.json").get(".")
    assert released_version is None or released_version == project_version


def test_release_please_policy() -> None:
    package = load_json(ROOT / "release-please-config.json")["packages"]["."]

    assert package["release-type"] == "python"
    assert package["package-name"] == "wlreviser"
    assert package["initial-version"] == "0.1.0"
    assert package["include-v-in-tag"] is True
    assert package["include-component-in-tag"] is False
    assert {
        "type": "toml",
        "path": "uv.lock",
        "jsonpath": "$.package[?(@.name == 'wlreviser')].version",
    } in package["extra-files"]