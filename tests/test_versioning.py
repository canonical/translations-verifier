import json
import tomllib
from pathlib import Path
from typing import Any

import yaml

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
        "jsonpath": "$.package[?(@.name.value == 'wlreviser')].version",
    } in package["extra-files"]


def test_release_please_dispatches_ci_for_release_pull_request() -> None:
    workflow = yaml.load(
        (ROOT / ".github/workflows/release-please.yaml").read_text(), Loader=yaml.BaseLoader
    )
    dispatch = workflow["jobs"]["release"]["steps"][1]

    assert dispatch["if"] == "steps.release.outputs.prs_created == 'true'"
    assert "'.headBranchName'" in dispatch["run"]
    assert '"repos/${GITHUB_REPOSITORY}/actions/workflows/ci.yaml/dispatches"' in dispatch["run"]
    assert '-f ref="$release_branch"' in dispatch["run"]
    assert "gh workflow run" not in dispatch["run"]
