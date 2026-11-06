"""Authenticated HTTP access to Earth Data Hub with retries and request accounting."""

import asyncio
import random
import threading

import aiohttp

from . import quota


class Fatal(Exception):
    """An error retrying cannot fix (bad key, access denied, quota exhausted on the server)."""


class QuotaReached(Exception):
    """The local safety limit for this month's quota was reached."""


class RequestCounter:
    """Counts requests for one key; flushes to the shared usage file in batches."""

    def __init__(self, fingerprint: str, limit: int | None = None, flush_every: int = 50):
        self.fp = fingerprint
        self.limit = limit
        self.flush_every = flush_every
        self._pending = 0
        self._lock = threading.Lock()
        self.session_total = 0
        self.month_used = quota.used(fingerprint)

    def reserve(self) -> None:
        with self._lock:
            if self.limit is not None and self.month_used + 1 > self.limit:
                raise QuotaReached(f"local quota limit {self.limit} reached ({self.month_used} used this month)")
            self.month_used += 1
            self.session_total += 1
            self._pending += 1
            flush = self._pending >= self.flush_every
        if flush:
            self.flush()

    def flush(self) -> None:
        with self._lock:
            n, self._pending = self._pending, 0
        if n:
            total = quota.add(self.fp, n)
            with self._lock:
                # other jobs may have used requests meanwhile: resync
                self.month_used = max(self.month_used, total + self._pending)


def proxy_from_env() -> str | None:
    import os
    from urllib.parse import urlparse

    if any(h.strip() and DATA_HOST.endswith(h.strip().lstrip("*.")) for h in
           (os.environ.get("NO_PROXY") or os.environ.get("no_proxy") or "").split(",")):
        return None
    p = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    return p if p and urlparse(p).scheme else None


DATA_HOST = "data.earthdatahub.destine.eu"


class EDHClient:
    def __init__(self, secret: str, counter: RequestCounter | None = None, connections: int = 8,
                 timeout: float = 300, retries: int = 6):
        self.secret = secret
        self.counter = counter
        self.connections = connections
        self.timeout = timeout
        self.retries = retries
        self.session: aiohttp.ClientSession | None = None
        self.bytes = 0
        self.on_retry = None  # optional callback(msg)

    async def __aenter__(self):
        self.session = aiohttp.ClientSession(
            headers={"Authorization": f"Bearer {self.secret}", "User-Agent": "edh-downloader"},
            timeout=aiohttp.ClientTimeout(total=self.timeout),
            connector=aiohttp.TCPConnector(limit=max(1, self.connections), ttl_dns_cache=600),
            trust_env=False,  # True would also inject ~/.netrc auth and clash with the Bearer header
        )
        self.proxy = proxy_from_env()
        return self

    async def __aexit__(self, *exc):
        if self.session:
            await self.session.close()
        if self.counter:
            self.counter.flush()

    async def get(self, url: str, missing_ok: bool = True) -> bytes | None:
        """GET ``url``. Returns ``None`` for 404 when ``missing_ok`` (absent Zarr chunk = fill value)."""
        for attempt in range(1, self.retries + 1):
            if self.counter:
                self.counter.reserve()
            try:
                async with self.session.get(url, proxy=self.proxy) as r:
                    if r.status == 404 and missing_ok:
                        return None
                    if r.status == 404:
                        raise Fatal(f"HTTP 404 for {url}: not found")
                    if r.status == 401:
                        raise Fatal(f"HTTP 401 for {url}: API key rejected")
                    if r.status == 403:
                        raise Fatal(f"HTTP 403 for {url}: access denied or server-side quota exhausted")
                    if r.status == 429 or r.status >= 500:
                        raise aiohttp.ClientResponseError(r.request_info, r.history, status=r.status,
                                                          message=r.reason or "")
                    r.raise_for_status()
                    data = await r.read()
                    self.bytes += len(data)
                    return data
            except (Fatal, QuotaReached):
                raise
            except Exception as e:
                if attempt == self.retries:
                    raise Fatal(f"giving up on {url} after {attempt} attempts: {type(e).__name__} {e}") from e
                wait = min(300, 3 * 2 ** attempt) * (0.75 + random.random() / 2)
                if self.on_retry:
                    self.on_retry(f"retry {attempt}/{self.retries - 1} in {wait:.0f}s for {url}: "
                                  f"{type(e).__name__} {getattr(e, 'status', '')} {e}")
                await asyncio.sleep(wait)
        raise AssertionError("unreachable")
