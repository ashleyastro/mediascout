"""Polite HTTP client: robots.txt aware, per-host concurrency and pacing, size limits."""

from __future__ import annotations

import asyncio
import gzip
import logging
import time
from collections import defaultdict
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import httpx

from .config import Settings

log = logging.getLogger(__name__)

MAX_BYTES = 6 * 1024 * 1024


class Fetcher:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        self._transport = transport
        self._client: httpx.AsyncClient | None = None
        self._global = asyncio.Semaphore(settings.max_concurrency)
        self._host_sems: dict[str, asyncio.Semaphore] = defaultdict(
            lambda: asyncio.Semaphore(settings.per_host_concurrency))
        self._host_last: dict[str, float] = defaultdict(float)
        self._host_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._robots: dict[str, RobotFileParser | None] = {}
        self._robots_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def __aenter__(self) -> "Fetcher":
        self._client = httpx.AsyncClient(
            headers={
                "User-Agent": self.settings.user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en,fr;q=0.8,de;q=0.7,es;q=0.6,it;q=0.5,nl;q=0.5",
            },
            timeout=self.settings.request_timeout,
            follow_redirects=True,
            transport=self._transport,
        )
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._client:
            await self._client.aclose()

    @property
    def client(self) -> httpx.AsyncClient:
        assert self._client is not None, "Fetcher used outside 'async with'"
        return self._client

    async def _allowed(self, url: str) -> bool:
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        async with self._robots_locks[origin]:
            if origin not in self._robots:
                parser: RobotFileParser | None = RobotFileParser()
                try:
                    resp = await self.client.get(origin + "/robots.txt")
                    if resp.status_code >= 400:
                        parser = None  # no robots.txt: everything allowed
                    else:
                        parser.parse(resp.text.splitlines())  # type: ignore[union-attr]
                except httpx.HTTPError:
                    parser = None
                self._robots[origin] = parser
        parser = self._robots[origin]
        if not self.settings.respect_robots:
            return True
        return parser is None or parser.can_fetch(self.settings.user_agent, url)

    async def prime_robots(self, url: str) -> None:
        await self._allowed(url)

    def robots_sitemaps(self, url: str) -> list[str]:
        parsed = urlparse(url)
        parser = self._robots.get(f"{parsed.scheme}://{parsed.netloc}")
        return list(parser.site_maps() or []) if parser else []

    async def _pace(self, host: str) -> None:
        delay = self.settings.per_host_delay_ms / 1000
        async with self._host_locks[host]:
            wait = self._host_last[host] + delay - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._host_last[host] = time.monotonic()

    async def get_text(self, url: str, check_robots: bool = True) -> str | None:
        """GET a URL and return its decoded body, or None on any failure."""
        if check_robots and not await self._allowed(url):
            log.debug("robots.txt disallows %s", url)
            return None
        host = urlparse(url).netloc
        async with self._global, self._host_sems[host]:
            await self._pace(host)
            for attempt in range(2):
                try:
                    async with self.client.stream("GET", url) as resp:
                        if resp.status_code == 429 or resp.status_code >= 500:
                            if attempt == 0:
                                await asyncio.sleep(2.0)
                                continue
                            return None
                        if resp.status_code >= 400:
                            return None
                        chunks, size = [], 0
                        async for chunk in resp.aiter_bytes():
                            size += len(chunk)
                            if size > MAX_BYTES:
                                return None
                            chunks.append(chunk)
                        body = b"".join(chunks)
                        if body[:2] == b"\x1f\x8b":
                            body = gzip.decompress(body)
                        encoding = resp.encoding or "utf-8"
                        return body.decode(encoding, errors="replace")
                except (httpx.HTTPError, OSError, gzip.BadGzipFile) as exc:
                    log.debug("fetch failed %s: %s", url, exc)
                    if attempt == 0:
                        await asyncio.sleep(1.0)
                        continue
                    return None
        return None

    async def get_json(self, url: str, params: dict[str, str] | None = None) -> object | None:
        """GET a JSON API (no robots check; APIs are not crawled pages)."""
        try:
            resp = await self.client.get(url, params=params)
            if resp.status_code != 200:
                log.warning("API %s returned %s", url, resp.status_code)
                return None
            return resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("API call failed %s: %s", url, exc)
            return None
