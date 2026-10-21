"""News-Beobachtung: Schlagzeilen sammeln, Trends erkennen, passende Coins finden."""
import html
import logging
import re
import time
import xml.etree.ElementTree as ET
from collections import Counter
from urllib.parse import quote

from .chains import DEX_TO_CHAIN, norm

log = logging.getLogger("news")



def parse_rss(xml_text: str) -> list[dict]:
    items = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return items
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        if not title:
            continue
        items.append({
            "title": html.unescape(title),
            "link": (it.findtext("link") or "").strip(),
            "source": (it.findtext("source") or "").strip(),
            "date": (it.findtext("pubDate") or "").strip(),
        })
    return items


class News:
    def __init__(self, http, settings, db, ai, market):
        self.http = http
        self.s = settings
        self.db = db
        self.ai = ai
        self.market = market

    @property
    def cfg(self):
        return self.s.section("news")

    async def headlines(self) -> list[dict]:
        out = []
        for url in self.cfg.get("feeds", []):
            txt = await self.http.get_text(url)
            if txt:
                out.extend(parse_rss(txt)[:40])
        # Duplikate entfernen
        seen, uniq = set(), []
        for i in out:
            k = i["title"].lower()[:80]
            if k not in seen:
                seen.add(k)
                uniq.append(i)
        return uniq

    async def update_trends(self) -> list[dict]:
        items = await self.headlines()
        titles = [i["title"] for i in items]
        if not titles:
            return self.db.kv_get("trends", [])
        trends = await self.ai.extract_trends(titles) if self.ai.enabled else None
        if not trends:
            return self.db.kv_get("trends", [])
        for t in trends:
            t["ts"] = time.time()
        self.db.kv_set("trends", trends)
        self.db.kv_set("headlines", titles[:200])
        log.info("Trends: %s", ", ".join(t["keyword"] for t in trends))
        return trends

    async def coins_for_trends(self, trends: list[dict], max_age_h: float) -> list[tuple[str, str, str]]:
        """Sucht auf DexScreener nach frischen Coins, die zu den Trend-Stichwörtern passen."""
        out = []
        chains = set(self.s.chains)
        for t in trends:
            kw = t["keyword"].strip()
            if len(kw) < 3:
                continue
            kw_l = kw.lower().replace(" ", "")
            for p in await self.market.search(kw):
                chain = DEX_TO_CHAIN.get(p.get("chainId", ""))
                if chain not in chains:
                    continue
                bt = p.get("baseToken") or {}
                name = (bt.get("name", "") + bt.get("symbol", "")).lower().replace(" ", "")
                created = p.get("pairCreatedAt") or 0
                if kw_l not in name or not created:
                    continue
                if (time.time() * 1000 - created) / 3.6e6 > max_age_h:
                    continue
                out.append((chain, norm(chain, bt.get("address", "")), f"News-Trend: {kw}"))
        return out

    def match_trend(self, name: str, symbol: str) -> dict | None:
        text = (name + " " + symbol).lower().replace(" ", "")
        for t in self.db.kv_get("trends", []):
            kw = t["keyword"].lower().replace(" ", "")
            if len(kw) >= 3 and kw in text:
                return t
        return None

    async def news_for(self, query: str, n=5) -> list[str]:
        """Aktuelle Google-News-Schlagzeilen zu einem Suchbegriff."""
        url = f"https://news.google.com/rss/search?q={quote(query)}+when:7d&hl=en-US&gl=US&ceid=US:en"
        txt = await self.http.get_text(url)
        return [i["title"] for i in parse_rss(txt or "")[:n]]
