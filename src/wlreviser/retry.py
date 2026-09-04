"""Shared exactly-once transient retry policy."""

import asyncio
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import httpx

TRANSIENT_HTTP_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})


class RetryableHttpStatus(Exception):
    """Internal signal for a transient HTTP status."""

    def __init__(self, status_code: int, retry_after: float = 0) -> None:
        super().__init__(f"transient HTTP status {status_code}")
        self.status_code = status_code
        self.retry_after = retry_after


def retry_after_seconds(value: str | None, *, now: datetime | None = None) -> float:
    """Parse Retry-After seconds or an HTTP date."""
    if value is None:
        return 0
    try:
        return max(0, float(value))
    except ValueError:
        pass
    try:
        retry_at = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        return 0
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=UTC)
    return max(0, (retry_at - (now or datetime.now(UTC))).total_seconds())


def is_transient(error: BaseException) -> bool:
    """Classify transport failures and the explicit transient status set."""
    return isinstance(error, (httpx.TimeoutException, httpx.TransportError, RetryableHttpStatus))


def delay_for(error: BaseException) -> float:
    return error.retry_after if isinstance(error, RetryableHttpStatus) else 0


async def retry_async[ResultT](
    operation: Callable[[], Awaitable[ResultT]],
    *,
    transient: Callable[[BaseException], bool] = is_transient,
    delay: Callable[[BaseException], float] = delay_for,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> ResultT:
    """Run an async operation and retry it at most once when transient."""
    try:
        return await operation()
    except Exception as exc:
        if not transient(exc):
            raise
        await sleep(delay(exc))
        return await operation()


def retry_sync[ResultT](
    operation: Callable[[], ResultT],
    *,
    transient: Callable[[BaseException], bool],
    delay: Callable[[BaseException], float] = delay_for,
    sleep: Callable[[float], None] = time.sleep,
) -> ResultT:
    """Run a synchronous operation and retry it at most once when transient."""
    try:
        return operation()
    except Exception as exc:
        if not transient(exc):
            raise
        sleep(delay(exc))
        return operation()