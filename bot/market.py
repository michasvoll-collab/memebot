"""Marktdaten & Entdeckung neuer Coins (DexScreener + GeckoTerminal)."""
import logging
import time
from urllib.parse import quote

from .chains import CHAINS, DEX_TO_CHAIN, GECKO_TO_CHAIN, norm

log = logging.getLogger("market")
DS = "https://api.dexscreener.com"
GT = "https://api.geckoterminal.com/api/v2"

# Stable-/Haupt-Coins, die nie als "Meme Coin" gemeldet werden sollen
IGNORE_SYMBOLS = {"SOL", "WSOL", "ETH", "WETH", "BNB", "WBNB", "USDC", "USDT", "DAI", "WBTC", "CBBTC",
                  "ARB", "USDE", "FDUSD", "PYUSD", "JUP", "BUSD", "STETH", "USD1"}


class Market:
    def __init__(self, http, settings):
        self.http = http
        self.s = settings

    # ---------------- Entdeckung ----------------
    async def discover(self) -> list[tuple[str, str, str]]:
        """Liefert Liste (chain, address, quelle) frischer Coins."""
        found: list[tuple[str, str, str]] = []
        chains = set(self.s.chains)

        for path, label in (("/token-profiles/latest/v1", "DexScreener neu"),
                            ("/token-boosts/latest/v1", "DexScreener Boost"),
                            ("/community-takeovers/latest/v1", "Community Takeover")):
            data = await self.http.get_json(DS + path)
            for item in data or []:
                chain = DEX_TO_CHAIN.get(item.get("chainId", ""))
                addr = item.get("tokenAddress")
                if chain in chains and addr:
                    found.append((chain, norm(chain, addr), label))

        for chain in self.s.chains:
            net = CHAINS[chain]["gecko"]
            data = await self.http.get_json(f"{GT}/networks/{net}/new_pools?page=1",
                                            headers={"Accept": "application/json;version=20230302"})
            for pool in (data or {}).get("data", []):
                try:
                    tid = pool["relationships"]["base_token"]["data"]["id"]  # "<net>_<addr>"
                    net_id, addr = tid.split("_", 1)
                    c = GECKO_TO_CHAIN.get(net_id)
                    if c in chains:
                        found.append((c, norm(c, addr), "Neuer Pool"))
                except (KeyError, ValueError, TypeError):
                    continue
        # Duplikate entfernen
        seen, out = set(), []
        for f in found:
            if (f[0], f[1]) not in seen:
                seen.add((f[0], f[1]))
                out.append(f)
        return out

    async def find_token(self, address: str) -> tuple[str, dict] | None:
        """Findet Chain + liquidestes Paar zu einer Token-Adresse (für /check)."""
        data = await self.http.get_json(f"{DS}/latest/dex/tokens/{address}")
        best = None
        for p in (data or {}).get("pairs") or []:
            chain = DEX_TO_CHAIN.get(p.get("chainId", ""))
            if not chain:
                continue
            if norm(chain, (p.get("baseToken") or {}).get("address", "")) != norm(chain, address):
                continue
            if best is None or liq(p) > liq(best[1]):
                best = (chain, p)
        return best

    async def search(self, query: str) -> list[dict]:
        data = await self.http.get_json(f"{DS}/latest/dex/search?q={quote(query)}")
        return (data or {}).get("pairs") or []

    # ---------------- Paar-Daten ----------------
    async def pairs_for_tokens(self, chain: str, addresses: list[str]) -> dict[str, dict]:
        """Bestes (liquidestes) Paar je Token. Bis zu 30 Adressen pro Anfrage."""
        result: dict[str, dict] = {}
        dex_chain = CHAINS[chain]["dexscreener"]
        for i in range(0, len(addresses), 30):
            batch = addresses[i:i + 30]
            data = await self.http.get_json(f"{DS}/tokens/v1/{dex_chain}/{','.join(batch)}")
            for p in data or []:
                addr = norm(chain, (p.get("baseToken") or {}).get("address", ""))
                if addr not in [norm(chain, b) for b in batch]:
                    continue  # Token ist nur Quote-Seite
                best = result.get(addr)
                if best is None or liq(p) > liq(best):
                    result[addr] = p
        return result

    async def pair_for_token(self, chain: str, address: str) -> dict | None:
        return (await self.pairs_for_tokens(chain, [address])).get(norm(chain, address))


# ---------------- Hilfsfunktionen ----------------
def liq(p: dict) -> float:
    return float((p.get("liquidity") or {}).get("usd") or 0)


def num(d: dict | None, *keys, default=0.0) -> float:
    cur = d or {}
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
    try:
        return float(cur)
    except (TypeError, ValueError):
        return default


def pair_metrics(p: dict) -> dict:
    created = p.get("pairCreatedAt") or 0
    age_min = (time.time() * 1000 - created) / 60000 if created else None
    buys = num(p, "txns", "h1", "buys")
    sells = num(p, "txns", "h1", "sells")
    socials = (p.get("info") or {}).get("socials") or []
    websites = (p.get("info") or {}).get("websites") or []
    return {
        "name": (p.get("baseToken") or {}).get("name", "?"),
        "symbol": (p.get("baseToken") or {}).get("symbol", "?"),
        "price": num(p, "priceUsd"),
        "liquidity": liq(p),
        "mcap": num(p, "marketCap") or num(p, "fdv"),
        "fdv": num(p, "fdv"),
        "age_min": age_min,
        "vol_5m": num(p, "volume", "m5"),
        "vol_1h": num(p, "volume", "h1"),
        "vol_24h": num(p, "volume", "h24"),
        "buys_1h": buys, "sells_1h": sells,
        "txns_1h": buys + sells,
        "buy_sell_ratio": buys / sells if sells else (buys if buys else 0),
        "chg_5m": num(p, "priceChange", "m5"),
        "chg_1h": num(p, "priceChange", "h1"),
        "chg_24h": num(p, "priceChange", "h24"),
        "dex": p.get("dexId", "?"),
        "url": p.get("url", ""),
        "pair": p.get("pairAddress", ""),
        "socials": [{"type": s.get("type") or s.get("platform", "link"), "url": s.get("url") or s.get("handle", "")}
                    for s in socials],
        "websites": [w.get("url") for w in websites if w.get("url")],
        "has_profile": bool(p.get("info")),
        "boosts": num(p, "boosts", "active"),
    }


def check_market(m: dict, cfg: dict) -> list[str]:
    """Gibt Liste der NICHT erfüllten Markt-Kriterien zurück (leer = alles ok)."""
    fails = []
    if m["symbol"].upper() in IGNORE_SYMBOLS:
        return ["Kein Meme Coin (Basis-/Stablecoin)"]
    c = cfg
    if m["liquidity"] < c.get("min_liquidity_usd", 0):
        fails.append(f"Liquidität zu niedrig (${m['liquidity']:,.0f})")
    if m["liquidity"] > c.get("max_liquidity_usd", 1e18):
        fails.append("Liquidität zu hoch (kein Meme-Frühphase)")
    if m["mcap"] < c.get("min_market_cap_usd", 0):
        fails.append(f"Market Cap zu niedrig (${m['mcap']:,.0f})")
    if m["mcap"] > c.get("max_market_cap_usd", 1e18):
        fails.append(f"Market Cap zu hoch (${m['mcap']:,.0f})")
    if m["age_min"] is None:
        fails.append("Alter unbekannt")
    else:
        if m["age_min"] < c.get("min_age_minutes", 0):
            fails.append(f"Zu jung ({m['age_min']:.0f} min)")
        if m["age_min"] > c.get("max_age_hours", 1e9) * 60:
            fails.append(f"Zu alt ({m['age_min'] / 60:.0f} h)")
    if m["vol_1h"] < c.get("min_volume_1h_usd", 0):
        fails.append(f"Volumen 1h zu niedrig (${m['vol_1h']:,.0f})")
    if m["txns_1h"] < c.get("min_txns_1h", 0):
        fails.append(f"Zu wenige Trades 1h ({m['txns_1h']:.0f})")
    if m["buy_sell_ratio"] < c.get("min_buy_sell_ratio_1h", 0):
        fails.append(f"Mehr Verkäufe als Käufe ({m['buy_sell_ratio']:.2f})")
    if m["buy_sell_ratio"] > c.get("max_buy_sell_ratio_1h", 1e9):
        fails.append(f"Verdächtig viele Käufe ({m['buy_sell_ratio']:.1f}x mehr als Verkäufe – Bot-Volumen?)")
    if m["chg_1h"] > c.get("max_price_rise_1h_pct", 1e9):
        fails.append(f"Schon zu stark gepumpt (+{m['chg_1h']:.0f} % in 1h)")
    if m["chg_1h"] < c.get("max_price_drop_1h_pct", -100):
        fails.append(f"Preis bricht ein ({m['chg_1h']:.0f} % in 1h)")
    turnover = m["vol_1h"] / m["liquidity"] if m["liquidity"] else 0
    if turnover > c.get("max_turnover_1h", 1e9):
        fails.append(f"Volumen {turnover:.1f}x der Liquidität in 1h (Wash-Trading/Dump-Gefahr)")
    if m["chg_5m"] > c.get("max_chg_5m_pct", 1e9):
        fails.append(f"Gerade Spike +{m['chg_5m']:.0f} % in 5 min – nicht hinterherkaufen")
    if m["chg_5m"] < c.get("min_chg_5m_pct", -100):
        fails.append(f"Fällt gerade ({m['chg_5m']:.0f} % in 5 min)")
    return fails
