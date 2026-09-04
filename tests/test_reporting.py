import json
import stat
from pathlib import Path

import pytest

from tests.test_applying import report
from wlreviser.errors import PreflightError
from wlreviser.reporting import load_report, write_report


def test_report_round_trip_is_atomic_and_private(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "report.json"
    value = report()

    write_report(value, path)

    assert load_report(path) == value
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["schema_version"] == 2
    assert raw["items"][0]["suggested_translation"] == "Hallo"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_report_loader_rejects_unsupported_version_without_echoing_values(tmp_path: Path) -> None:
    path = tmp_path / "report.json"
    raw = json.loads(report().model_dump_json())
    raw["schema_version"] = 999
    raw["repository"] = "SENTINEL_TOKEN"
    path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(PreflightError) as raised:
        load_report(path)

    assert "SENTINEL_TOKEN" not in str(raised.value)