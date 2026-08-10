from __future__ import annotations

import asyncio

from partsouq_crawler.crawl import rate_limit


def test_rate_limiter_serializes_concurrent_requests_at_fixed_intervals(monkeypatch) -> None:
    now = 100.0
    sleeps: list[float] = []

    def fake_monotonic() -> float:
        return now

    async def fake_sleep(seconds: float) -> None:
        nonlocal now
        sleeps.append(seconds)
        now += seconds

    monkeypatch.setattr(rate_limit, "monotonic", fake_monotonic)
    monkeypatch.setattr(rate_limit.asyncio, "sleep", fake_sleep)
    limiter = rate_limit.HostRateLimiter(30.0)

    async def scenario() -> None:
        await asyncio.gather(*(limiter.wait() for _ in range(3)))

    asyncio.run(scenario())

    assert sleeps == [30.0, 30.0]
    assert now == 160.0
