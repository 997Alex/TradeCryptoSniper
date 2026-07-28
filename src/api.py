from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import httpx

from src.logger import get_logger

log = get_logger("api")

MAX_RETRY_DELAY_SEC = 2.0


def as_str_list(raw: Any) -> list[str]:
    """Gamma returns `outcomePrices` and `clobTokenIds` as either a JSON array or a
    JSON-encoded string, depending on the endpoint. Normalise both, and anything
    unexpected to an empty list."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return []
    if isinstance(raw, (list, tuple)):
        return [str(x) for x in raw]
    return []


class RateLimiter:
    """Serialises callers so no two requests leave less than `1 / rps` apart."""

    def __init__(self, rps: float):
        self._interval = 1.0 / rps if rps > 0 else 0.0
        self._next_at = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        if not self._interval:
            return
        async with self._lock:
            now = time.monotonic()
            if self._next_at > now:
                await asyncio.sleep(self._next_at - now)
                now = self._next_at
            self._next_at = now + self._interval


class ApiClient:
    """Rate-limited JSON GET client. Returns None on any failure rather than raising,
    so callers never have to guard against an API hiccup unwinding the trading loop."""

    def __init__(self, base_url: str, rate_limit_rps: float, timeout: float = 15.0):
        self._base = base_url.rstrip("/")
        self._http = httpx.AsyncClient(base_url=self._base, timeout=timeout)
        self._limiter = RateLimiter(rate_limit_rps)

    async def get_json(self, path: str, params: dict | None = None, attempts: int = 2) -> Any | None:
        for attempt in range(attempts):
            await self._limiter.acquire()
            try:
                resp = await self._http.get(path, params=params)
            except httpx.HTTPError as exc:
                log.warning("http_error", path=path, error=str(exc))
                return None

            if resp.status_code == 200:
                try:
                    return resp.json()
                except ValueError:
                    log.warning("http_bad_json", path=path)
                    return None

            retryable = resp.status_code == 429 or resp.status_code >= 500
            if retryable and attempt + 1 < attempts:
                delay = self._retry_delay(resp, attempt)
                log.warning("http_retry", path=path, status=resp.status_code, delay=round(delay, 2))
                await asyncio.sleep(delay)
                continue

            log.warning("http_status", path=path, status=resp.status_code)
            return None
        return None

    @staticmethod
    def _retry_delay(resp: httpx.Response, attempt: int) -> float:
        header = resp.headers.get("Retry-After")
        if header:
            try:
                return min(float(header), MAX_RETRY_DELAY_SEC)
            except ValueError:
                pass
        return min(0.5 * (2**attempt), MAX_RETRY_DELAY_SEC)

    async def aclose(self) -> None:
        await self._http.aclose()
