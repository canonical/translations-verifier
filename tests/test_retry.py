from datetime import UTC, datetime

import httpx
import pytest

from wlreviser.retry import retry_after_seconds, retry_async


async def test_retry_async_retries_exactly_once_for_transport_failure() -> None:
    calls = 0
    sleeps: list[float] = []

    async def operation() -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ReadTimeout("timeout")
        return "ok"

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    assert await retry_async(operation, sleep=sleep) == "ok"
    assert calls == 2
    assert sleeps == [0]


async def test_retry_async_does_not_retry_validation_errors() -> None:
    calls = 0

    async def operation() -> None:
        nonlocal calls
        calls += 1
        raise ValueError("bad payload")

    with pytest.raises(ValueError):
        await retry_async(operation)
    assert calls == 1


def test_retry_after_supports_seconds_and_http_dates() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)

    assert retry_after_seconds("1.5", now=now) == 1.5
    assert retry_after_seconds("Thu, 01 Jan 2026 00:00:10 GMT", now=now) == 10
    assert retry_after_seconds("invalid", now=now) == 0