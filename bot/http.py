"""Gemeinsamer HTTP-Client mit Rate-Limit pro Host und Wiederholungen."""
from __future__ import annotations
import asyncio
import logging
import time
from urllib.parse import urlparse

import httpx

log = logging.getLogger("http")

# Mindestabstand zwischen Anfragen pro Host (Sekunden) – hält kostenlose Limits ein
HOST_INTERVAL = {
    "api.dexscreener.com": 0.25,
    "api.geckoterminal.com": 2.2,     # ~30/min
    "api.gopluslabs.io": 2.1,         # ~30/min ohne Key
    "api.rugcheck.xyz": 0.6,
    "api.honeypot.is": 0.5,
    "api.etherscan.io": 0.25,
    "news.google.com": 1.0,
    "api.mainnet-beta.solana.com": 0.35,   # öffentlicher RPC: ~40 Anfragen/10 s
}
DEFAULT_INTERVAL = 0.2


class Http:
    def __init__(self):
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(20.0),
            headers={"User-Agent": "Mozilla/5.0 (MemeBot; +https://t.me)", "Accept": "application/json"},
            follow_redirects=True,
        )
        self._locks: dict[str, asyncio.Lock] = {}
        self._last: dict[str, float] = {}

    async def _throttle(self, host: str):
        lock = self._locks.setdefault(host, asyncio.Lock())
        async with lock:
            gap = HOST_INTERVAL.get(host, DEFAULT_INTERVAL)
            wait = self._last.get(host, 0) + gap - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last[host] = time.monotonic()

    async def request(self, method: str, url: str, *, retries: int = 3, **kw) -> httpx.Response | None:
        host = urlparse(url).hostname or ""
        for attempt in range(retries):
            await self._throttle(host)
            try:
                r = await self.client.request(method, url, **kw)
                if r.status_code == 429 or r.status_code >= 500:
                    backoff = 2 ** attempt * 3
                    log.warning("%s %s -> %s, warte %ss", method, host, r.status_code, backoff)
                    await asyncio.sleep(backoff)
                    continue
                return r
            except httpx.HTTPError as e:
                log.warning("%s %s Fehler: %s", method, host, e)
                await asyncio.sleep(2 ** attempt)
        return None

    async def get_json(self, url: str, **kw):
        r = await self.request("GET", url, **kw)
        if r is None or r.status_code != 200:
            if r is not None:
                log.debug("GET %s -> %s %s", url, r.status_code, r.text[:200])
            return None
        try:
            return r.json()
        except ValueError:
            return None

    async def post_json(self, url: str, payload, **kw):
        r = await self.request("POST", url, json=payload, **kw)
        if r is None or r.status_code != 200:
            return None
        try:
            return r.json()
        except ValueError:
            return None

    async def get_text(self, url: str, **kw) -> str | None:
        r = await self.request("GET", url, headers={"Accept": "*/*"}, **kw)
        if r is None or r.status_code != 200:
            return None
        return r.text

    async def close(self):
        await self.client.aclose()
