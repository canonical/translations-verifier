from typer.testing import CliRunner

from wlreviser.cli import app


def test_cli_lists_commands() -> None:
    result = CliRunner().invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "verify" in result.stdout
    assert "apply" in result.stdout


def test_verify_preflight_does_not_echo_secret_config_values(tmp_path: object) -> None:
    from pathlib import Path

    path = Path(str(tmp_path)) / "config.yaml"
    path.write_text("github:\n  token: SENTINEL_TOKEN\n", encoding="utf-8")

    result = CliRunner().invoke(
        app,
        ["verify", str(path), "https://github.com/canonical/repo/pull/1", "report.json"],
    )

    assert result.exit_code == 2
    assert "SENTINEL_TOKEN" not in result.output