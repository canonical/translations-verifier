"""Atomic verification report persistence."""

import os
import tempfile
from pathlib import Path

from pydantic import ValidationError

from wlreviser.errors import PreflightError
from wlreviser.models import VerificationReport


def write_report(report: VerificationReport, path: Path) -> None:
    """Write a private UTF-8 JSON report atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(report.model_dump_json(indent=2))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise


def load_report(path: Path) -> VerificationReport:
    """Load only a currently supported, internally consistent report."""
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise PreflightError(f"could not read verification report: {type(exc).__name__}") from exc
    try:
        return VerificationReport.model_validate_json(content)
    except ValidationError as exc:
        details = "; ".join(
            f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
            for error in exc.errors(include_url=False, include_input=False)
        )
        raise PreflightError(f"invalid or unsupported verification report: {details}") from exc