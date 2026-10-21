"""X/Twitter-Hype über twitterapi.io.

1. Beobachtet Promis/KOLs (Trump, Elon, CZ, bekannte Meme-Trader …) und macht aus ihren Posts Kandidaten.
2. Misst für jeden Coin, wie stark er gerade auf X erwähnt wird (Anzahl, verschiedene Accounts, Reichweite).
"""
import logging
import math
import re
import time

from .chains import DEX_TO_CHAIN, norm

log = logging.getLogger("xhype")
API = "https://api.twitterapi.io/twitter/tweet/advanced_search"
ADDR_RE = re.compile(r"\b(0x[a-fA-F0-9]{40}|[1-9A-HJ-NP-Za-km-z]{32,44})\b")
TICKER_RE = re.compile(r"\$([A-Za-z][A-Za-z0-9]{1,14})\b")


class XHype:
    def __init__(self, http, settings, db, ai, market):
        self.http, self.s, self.db, self.ai, self.market = http, settings, db, ai, market
        self.seen_ids: set[str] = set()   # Tweets nicht doppelt als Erwähnung zählen

    @property
    def cfg(self):
        return self.s.section("x")

    @property
    def key(self):
        return self.db.kv_get("x_key") or self.s.x_key

    @property
    def enabled(self):
        return bool(self.key) and self.cfg.get("enabled", True)

    async def search(self, query: str, pages: int = 1) -> list[dict] | None:
        """Neueste Tweets zu einer Suche. None = Fehler."""
        out, cursor = [], ""
        for _ in range(pages):
            r = await self.http.request("GET", API, params={"query": query, "queryType": "Latest", "cursor": cursor},
                                        headers={"X-API-Key": self.key}, retries=2)
            if r is None or r.status_code != 200:
                self.db.kv_set("x_status", {"ok": False, "code": r.status_code if r is not None else 0,
                                            "msg": (r.text[:200] if r is not None else "keine Antwort"), "ts": time.time()})
                return None if not out else out
            data = r.json()
            self.db.kv_set("x_status", {"ok": True, "ts": time.time()})
            for t in data.get("tweets") or []:
                a = t.get("author") or {}
                out.append({"id": str(t.get("id")), "text": t.get("text") or "", "user": a.get("userName") or "?",
                            "followers": int(a.get("followers") or a.get("followersCount") or 0),
                            "likes": int(t.get("likeCount") or 0), "views": int(t.get("viewCount") or 0),
                            "created": t.get("createdAt")})
            if not data.get("has_next_page"):
                break
            cursor = data.get("next_cursor") or ""
        return out

    # ---------------- Coin-Buzz ----------------
    async def buzz(self, symbol: str, address: str) -> dict:
        """Wie heiß ist der Coin gerade auf X? (letzte 60 Minuten)"""
        if not self.enabled:
            return {}
        since = int(time.time()) - 3600
        q = f'("{address}" OR ${symbol}) since_time:{since}' if len(symbol) >= 3 else f'"{address}" since_time:{since}'
        tweets = await self.search(q, pages=int(self.cfg.get("buzz_pages", 1)))
        if tweets is None:
            return {}
        users = {}
        for t in tweets:
            users[t["user"]] = max(users.get(t["user"], 0), t["followers"])
            if t["id"] not in self.seen_ids:
                self.seen_ids.add(t["id"])
                self.db.add_mention(address, "x", t["user"], 1 + math.log10(1 + t["followers"]) / 2)
        if len(self.seen_ids) > 50000:
            self.seen_ids.clear()
        reach = sum(users.values())
        big = sorted(((f, u) for u, f in users.items() if f >= 50_000), reverse=True)[:3]
        score = min(100, round(min(45, len(users) * 3) + max(0, math.log10(1 + reach) - 3) * 12 + (15 if big else 0)))
        return {"tweets": len(tweets), "accounts": len(users), "reach": reach, "score": score,
                "big": [f"@{u} ({f / 1000:.0f}K)" for f, u in big], "full": len(tweets) >= 20}

    # ---------------- Promis / KOLs beobachten ----------------
    async def kol_cycle(self) -> list[tuple[str, str, str]]:
        """Neue Posts der beobachteten Accounts -> Kandidaten (chain, address, quelle)."""
        if not self.enabled:
            return []
        accounts = self.cfg.get("accounts") or []
        last = self.db.kv_get("x_kol_since", int(time.time()) - 600)
        now = int(time.time())
        posts = []
        for i in range(0, len(accounts), 8):   # X-Suche hat eine Längenbegrenzung -> in Gruppen
            q = "(" + " OR ".join(f"from:{a}" for a in accounts[i:i + 8]) + f") since_time:{last}"
            res = await self.search(q)
            posts += res or []
        self.db.kv_set("x_kol_since", now)
        if not posts:
            return []
        feed = (self.db.kv_get("x_feed", []) + [{"user": p["user"], "text": p["text"][:200], "ts": now} for p in posts])[-30:]
        self.db.kv_set("x_feed", feed)

        found = []
        # 1) direkt genannte Contract-Adressen / Ticker
        for p in posts:
            for a in ADDR_RE.findall(p["text"]):
                ch = "solana" if not a.startswith("0x") else None
                pair = await self.market.find_token(a)
                if pair:
                    ch = pair[0]
                if ch:
                    found.append((ch, norm(ch, a), f"X: @{p['user']}"))
                    self.db.add_mention(norm(ch, a), "x", p["user"], 5)
        # 2) memebare Stichwörter (KI) -> frische Coins auf DexScreener suchen
        kws = await self.ai.keywords_from_posts(posts) if self.ai.enabled else []
        kws += [{"keyword": t, "user": p["user"]} for p in posts for t in TICKER_RE.findall(p["text"])]
        max_age_h = float(self.cfg.get("max_coin_age_hours", 6))
        chains = set(self.s.chains)
        seen = set()
        for k in kws[:12]:
            kw = str(k["keyword"]).strip().lstrip("$")
            if len(kw) < 3 or kw.lower() in seen:
                continue
            seen.add(kw.lower())
            for pr in await self.market.search(kw):
                ch = DEX_TO_CHAIN.get(pr.get("chainId", ""))
                bt = pr.get("baseToken") or {}
                created = pr.get("pairCreatedAt") or 0
                if ch not in chains or not created or (time.time() * 1000 - created) / 3.6e6 > max_age_h:
                    continue
                if kw.lower().replace(" ", "") not in (bt.get("name", "") + bt.get("symbol", "")).lower().replace(" ", ""):
                    continue
                addr = norm(ch, bt.get("address", ""))
                found.append((ch, addr, f"X-Trend: {kw} (@{str(k.get('user', '?')).lstrip('@')})"))
                self.db.add_mention(addr, "x", f"kol:{k.get('user')}", 3)
        trends = self.db.kv_get("trends", [])
        for k in kws[:8]:
            if k.get("why"):
                trends = [{"keyword": k["keyword"], "headline": f"X: {k.get('user')}", "why": k["why"], "ts": now}] + trends
        self.db.kv_set("trends", trends[:25])
        return found
