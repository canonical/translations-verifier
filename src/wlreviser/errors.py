"""Safe operational error types and rendering."""

import re
from collections.abc import Iterable


class WLReviserError(Exception):
    """Base class for errors safe to present at the CLI boundary."""


class PreflightError(WLReviserError):
    """A configuration or authentication requirement prevents a run."""


class ExternalServiceError(WLReviserError):
    """An external service failed without exposing its response body."""


class MalformedResponseError(ExternalServiceError):
    """An external service returned data that violates its contract."""


def safe_error_message(error: BaseException, secrets: Iterable[str] = ()) -> str:
    """Render a bounded message without headers, bodies, or known secret values."""
    if isinstance(error, WLReviserError):
        message = str(error)
    else:
        message = f"{type(error).__name__}: operation failed"
    for secret in secrets:
        if secret:
            message = message.replace(secret, "[redacted]")
    message = re.sub(r"[\r\n\t]+", " ", message)
    return message[:300]