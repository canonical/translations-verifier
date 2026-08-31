from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from contextlib import ExitStack
from pathlib import Path

from translations_verifier.ai import AiReviewer
from translations_verifier.config import (
    ConfigurationError,
    config_fingerprint,
    load_ai_config,
    load_project_config,
)
from translations_verifier.github import GitHubClient, GitHubError
from translations_verifier.models import OverallStatus
from translations_verifier.reporting import (
    render_github_annotations,
    render_markdown,
    write_json_report,
)
from translations_verifier.verifier import Verifier


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="translations-verifier",
        description="Advisory verification of ARB translation changes",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    config_parser = commands.add_parser("config", help="validate verifier configuration")
    config_commands = config_parser.add_subparsers(dest="config_command", required=True)
    validate_parser = config_commands.add_parser("validate", help="validate YAML configuration")
    validate_parser.add_argument("--project-config", type=Path, required=True)
    validate_parser.add_argument("--ai-config", type=Path)

    verify_parser = commands.add_parser(
        "verify", help="verify translation changes in a pull request"
    )
    verify_parser.add_argument("--project-config", type=Path, required=True)
    verify_parser.add_argument("--ai-config", type=Path)
    verify_parser.add_argument("--pr", required=True, help="pull request number or GitHub URL")
    verify_parser.add_argument(
        "--output",
        type=Path,
        default=Path("translation-verification.json"),
        help="JSON report path",
    )
    verify_parser.add_argument("--summary-output", type=Path, help="Markdown summary path")
    verify_parser.add_argument("--deterministic-only", action="store_true")
    verify_parser.add_argument("--github-annotations", action="store_true")
    verify_parser.add_argument("--github-token-env", default="GITHUB_TOKEN")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "config" and args.config_command == "validate":
            project = load_project_config(args.project_config)
            print(f"project configuration valid ({config_fingerprint(project)[:12]})")
            if args.ai_config:
                ai = load_ai_config(args.ai_config)
                print(f"AI configuration valid ({config_fingerprint(ai)[:12]})")
            return 0

        if args.command == "verify":
            project = load_project_config(args.project_config)
            ai_config = load_ai_config(args.ai_config) if args.ai_config else None
            with ExitStack() as stack:
                github = stack.enter_context(
                    GitHubClient(
                        project.repository,
                        token=os.environ.get(args.github_token_env),
                        api_url=os.environ.get("GITHUB_API_URL", "https://api.github.com"),
                    )
                )
                ai_reviewer = None
                if not args.deterministic_only and ai_config is not None:
                    ai_reviewer = stack.enter_context(
                        AiReviewer(ai_config, ai_config.resolve_api_key())
                    )
                report = Verifier(
                    project,
                    github,
                    ai_config=ai_config,
                    ai_reviewer=ai_reviewer,
                    deterministic_only=args.deterministic_only,
                ).verify(args.pr)
            write_json_report(report, args.output)
            summary = render_markdown(report)
            print(summary, end="")
            if args.summary_output:
                args.summary_output.parent.mkdir(parents=True, exist_ok=True)
                args.summary_output.write_text(summary, encoding="utf-8")
            if args.github_annotations:
                print(render_github_annotations(report), end="")
            return _status_exit_code(report.status)
    except (ConfigurationError, GitHubError, OSError) as exc:
        print(f"verification error: {exc}", file=sys.stderr)
        return 2

    parser.error("unsupported command")
    return 2


def _status_exit_code(status: OverallStatus) -> int:
    if status is OverallStatus.ACCEPTABLE:
        return 0
    if status is OverallStatus.UNACCEPTABLE:
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
