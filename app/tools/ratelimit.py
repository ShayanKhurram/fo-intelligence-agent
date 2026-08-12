"""Process-global async token buckets. The runner spec (plan §5) requires SEC EDGAR and
LinkedIn rate limits enforced globally across all leads in flight, not per-lead — module
level singletons give every coroutine in the process the same bucket."""
from __future__ import annotations

import asyncio
import time

from app.config import SETTINGS


class AsyncTokenBucket:
    def __init__(self, rate_per_sec: float, burst: float | None = None) -> None:
        self.rate = rate_per_sec
        self.capacity = burst if burst is not None else max(1.0, rate_per_sec)
        self._tokens = self.capacity
        self._last = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, cost: float = 1.0) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                elapsed = now - self._last
                self._last = now
                self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
                if self._tokens >= cost:
                    self._tokens -= cost
                    return
                wait = (cost - self._tokens) / self.rate
                await asyncio.sleep(wait)


SEC_BUCKET = AsyncTokenBucket(SETTINGS.runner.sec_rate_per_sec)
LINKEDIN_BUCKET = AsyncTokenBucket(SETTINGS.runner.linkedin_rate_per_sec)
GDELT_BUCKET = AsyncTokenBucket(SETTINGS.runner.gdelt_rate_per_sec)
SERPER_BUCKET = AsyncTokenBucket(SETTINGS.runner.serper_rate_per_sec)
SNOV_BUCKET = AsyncTokenBucket(SETTINGS.runner.snov_rate_per_sec)
