"""Command-line interface for WLReviser."""

import asyncio
import os
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from wlreviser.ai import OpenAITranslationReviewer
from wlreviser.applying import ApplyService
from wlreviser.config import ProjectConfig, load_config
from wlreviser.errors import PreflightError, safe_error_message
from wlreviser.github import GitHubClient
from wlreviser.models import VerificationReport
from wlreviser.rendering import render_apply, render_verification
from wlreviser.reporting import load_report, write_report
from wlreviser.verification import VerificationService
from wlreviser.weblate import WeblateGateway

app = typer.Typer(
    name="wlreviser",
    help="Verify and apply AI-reviewed Weblate translation changes.",
    no_args_is_help=True,
)
_stdout = Console()
_stderr = Console(stderr=True)


def _environment_value(name: str, *, required: bool) -> str | None:
    value = os.environ.get(name)
    if required and not value:
        raise PreflightError(f"{name} is required")
    return value or None


async def _run_verification(
    project: ProjectConfig,
    pull_request_url: str,
    github_token: str,
    weblate_token: str,
    ai_token: str | None,
) -> VerificationReport:
    async with (
        GitHubClient(github_token) as github,
        OpenAITranslationReviewer(project.ai, ai_token) as reviewer,
    ):
        weblate = WeblateGateway(project.weblate, weblate_token)
        return await VerificationService(project, github, weblate, reviewer).verify(
            pull_request_url
        )


@app.command()
def verify(
    config: Annotated[
        Path,
        typer.Argument(exists=True, dir_okay=False, readable=True, resolve_path=True),
    ],
    pull_request_url: Annotated[str, typer.Argument()],
    output: Annotated[Path, typer.Argument(dir_okay=False, resolve_path=True)],
) -> None:
    """Verify translation changes from a GitHub pull request."""
    secrets: list[str] = []
    try:
        project = load_config(config)
        github_token = _environment_value("GITHUB_TOKEN", required=True)
        weblate_token = _environment_value("WEBLATE_TOKEN", required=True)
        ai_token = _environment_value("WL_BOT_AI_TOKEN", required=False)
        assert github_token is not None
        assert weblate_token is not None
        secrets.extend((github_token, weblate_token, ai_token or ""))
        report = asyncio.run(
            _run_verification(
                project,
                pull_request_url,
                github_token,
                weblate_token,
                ai_token,
            )
        )
        write_report(report, output)
        render_verification(report, output, _stdout)
    except Exception as exc:
        _stderr.print(f"Error: {safe_error_message(exc, secrets)}", markup=False)
        raise typer.Exit(code=2) from exc


@app.command()
def apply(
    config: Annotated[
        Path,
        typer.Argument(exists=True, dir_okay=False, readable=True, resolve_path=True),
    ],
    report: Annotated[
        Path,
        typer.Argument(exists=True, dir_okay=False, readable=True, resolve_path=True),
    ],
    finding_ids: Annotated[list[str], typer.Argument()],
) -> None:
    """Apply selected report suggestions to Weblate."""
    secrets: list[str] = []
    try:
        project = load_config(config)
        verification_report = load_report(report)
        weblate_token = _environment_value("WEBLATE_TOKEN", required=True)
        assert weblate_token is not None
        secrets.append(weblate_token)
        gateway = WeblateGateway(project.weblate, weblate_token)
        result = ApplyService(project, gateway).apply(verification_report, finding_ids)
        render_apply(result, _stdout)
    except Exception as exc:
        _stderr.print(f"Error: {safe_error_message(exc, secrets)}", markup=False)
        raise typer.Exit(code=2) from exc