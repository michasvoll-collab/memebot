"""Kern-Logik: Coins entdecken, prüfen, bewerten und melden."""
import logging
import time
from dataclasses import dataclass, field

from .chains import CHAINS, norm
from .fmt import coin_message
from .market import check_market, pair_metrics
from .security import SecurityResult

log = logging.getLogger("scanner")

# Markt-Fehler, bei denen sich ein erneuter Check nicht lohnt
FATAL_MARKET = ("Kein Meme Coin", "Zu alt", "Market Cap zu hoch", "Liquidität zu hoch")


@dataclass
class Evaluation:
    chain: str
    address: str
    source: str
    metrics: dict = field(default_factory=dict)
    market_fails: list = field(default_factory=list)
    security: SecurityResult | None = None
    trend: dict | None = None
    news: list = field(default_factory=list)
    ai_text: str | None = None
    sec_score: int = 0
    market_score: int = 0
    hype_score: int = 0
    total: int = 0
    found: bool = True
    x: dict = field(default_factory=dict)      # X/Twitter-Buzz
    tgb: dict = field(default_factory=dict)    # Telegram-Erwähnungen

    @property
    def criteria_ok(self) -> bool:
        return self.found and not self.market_fails and self.security is not None and self.security.passed


def market_score(m: dict) -> int:
    """Punkte für gesundes, organisches Wachstum (aus Tag-1-Auswertung abgeleitet)."""
    s = 50
    liq, mcap = m["liquidity"], m["mcap"] or 1
    ratio = liq / mcap
    s += 10 if ratio >= 0.15 else (5 if ratio >= 0.08 else -10)          # gesunde Liquidität
    bs = m["buy_sell_ratio"]
    s += 10 if 0.9 <= bs <= 1.8 else (0 if bs <= 2.5 else -15)           # ausgeglichener Handel = echte Trader
    age = m["age_min"] or 0
    s += 10 if age >= 180 else (5 if age >= 60 else -5)                  # hat schon eine Weile überlebt
    chg = m["chg_1h"]
    s += 10 if -15 <= chg <= 60 else (0 if chg <= 120 else -15)          # ruhiger Anstieg statt Pump
    s += 5 if m["vol_1h"] >= liq * 0.3 else 0                            # es wird gehandelt
    s += 5 if 40_000 <= mcap <= 150_000 else 0                           # Tag 1: bester MC-Bereich
    types = {str(x.get("type", "")).lower() for x in m.get("socials", [])}
    s += 5 if "twitter" in types or "x" in types else -5
    s += 3 if "telegram" in types else 0
    s += 2 if m.get("websites") else -3
    return max(0, min(100, s))


HYPE_SOURCES = ("X:", "X-Trend", "Telegram:")


class Scanner:
    def __init__(self, s, db, market, security, news, ai, tg, xhype=None, tghype=None):
        self.s, self.db, self.market, self.sec = s, db, market, security
        self.news, self.ai, self.tg = news, ai, tg
        self.x, self.tgh = xhype, tghype
        self.paused = False
        self.ai_cache: dict[str, tuple[float, str | None, int | None]] = {}
        self.x_cache: dict[str, tuple[float, dict]] = {}

    def market_cfg(self, source: str) -> dict:
        """Markt-Regeln; Coins mit echtem X-/Telegram-Hype dürfen jünger sein (Schnellspur)."""
        c = self.s.section("market")
        if source.startswith(HYPE_SOURCES) and c.get("min_age_minutes_hype") is not None:
            c["min_age_minutes"] = c["min_age_minutes_hype"]
        return c

    async def x_buzz(self, symbol, address) -> dict:
        if not (self.x and self.x.enabled):
            return {}
        hit = self.x_cache.get(address)
        if hit and time.time() - hit[0] < 600:
            return hit[1]
        b = await self.x.buzz(symbol, address)
        self.x_cache[address] = (time.time(), b)
        return b

    def weights(self):
        w = self.s.section("score_weights") or {}
        return w.get("security", 0.35), w.get("market", 0.25), w.get("hype", 0.40)

    # ---------- komplette Bewertung eines Coins ----------
    async def evaluate(self, chain, address, source, pair=None, deep=True, force_ai=False) -> Evaluation:
        ev = Evaluation(chain, address, source)
        pair = pair or await self.market.pair_for_token(chain, address)
        if not pair:
            ev.found = False
            ev.market_fails = ["Noch kein handelbares Paar auf DexScreener"]
            return ev
        ev.metrics = m = pair_metrics(pair)
        ev.market_fails = check_market(m, self.market_cfg(source))
        ev.market_score = market_score(m)
        ev.trend = self.news.match_trend(m["name"], m["symbol"]) if self.news else None
        if ev.market_fails and not force_ai and not deep:
            return ev

        ev.security = await self.sec.check(chain, address, m["pair"])
        ev.sec_score = ev.security.score
        if ev.security.fatal and not force_ai:
            ev.total = 0
            return ev

        # ---- Hype: Trend, Profil, X-Buzz, Telegram-Erwähnungen ----
        hype = 20 + (20 if ev.trend else 0) + (8 if m.get("has_profile") else 0) + (4 if m.get("boosts") else 0)
        if ev.criteria_ok or force_ai:
            ev.x = await self.x_buzz(m["symbol"], address)
            ev.tgb = self.tgh.buzz(address) if self.tgh else {}
            hype += 0.5 * ev.x.get("score", 0) + 0.4 * ev.tgb.get("score", 0)
            if source.startswith(HYPE_SOURCES):
                hype += 10
            ev.news = await self.news.news_for(m["name"]) if self.news else []
            if ev.news:
                hype += 8
        hype = max(0, min(100, hype))

        ws, wm, wh = self.weights()
        best_case = ws * ev.sec_score + wm * ev.market_score + wh * max(hype, 0.5 * hype + 50)
        min_ms = self.s.get("min_market_score", default=0)
        ai_ok = force_ai or (ev.criteria_ok and ev.market_score >= min_ms
                              and best_case >= self.s.get("min_score", default=65))
        if ai_ok and self.ai.enabled:
            hit = self.ai_cache.get(address)
            ttl = self.s.get("ai", "cache_minutes", default=180) * 60
            if hit and time.time() - hit[0] < ttl:
                ev.ai_text, ai_hype = hit[1], hit[2]
            else:
                ev.ai_text, ai_hype = await self.ai.analyze_coin(self._ctx(ev, m, source))
                if ev.ai_text:
                    self.ai_cache[address] = (time.time(), ev.ai_text, ai_hype)
            if ai_hype is not None:
                hype = round(0.5 * hype + 0.5 * ai_hype)
        ev.hype_score = round(max(0, min(100, hype)))
        ev.total = round(ws * ev.sec_score + wm * ev.market_score + wh * ev.hype_score)
        ev.total -= int((self.s.section("source_penalty") or {}).get(source, 0))
        if ev.market_score < min_ms and not force_ai:
            ev.market_fails.append(f"Markt-Punkte zu niedrig ({ev.market_score} < {min_ms})")
        if ev.security.fatal:
            ev.total = 0  # Honeypot & Co. -> egal wie gut der Rest aussieht
        return ev

    def _ctx(self, ev, m, source):
        return {
            "name": m["name"], "symbol": m["symbol"], "chain": CHAINS[ev.chain]["name"],
            "contract": ev.address, "alter": f"{(m['age_min'] or 0):.0f} Minuten",
            "market_cap_usd": round(m["mcap"]), "liquiditaet_usd": round(m["liquidity"]),
            "volumen_1h_usd": round(m["vol_1h"]), "preis_aenderung_5m_1h_pct": [m["chg_5m"], m["chg_1h"]],
            "socials": m["socials"], "websites": m["websites"],
            "dexscreener_boosts": m.get("boosts"), "gefunden_ueber": source,
            "x_twitter_letzte_stunde": ev.x or "nicht gemessen",
            "telegram_kanaele_letzte_stunde": ev.tgb or "nicht gemessen",
            "passender_news_trend": ev.trend, "google_news_zum_namen": ev.news,
            "sicherheits_check": {"probleme": ev.security.hard_fails, "warnungen": ev.security.warnings,
                                  "fakten": ev.security.facts},
        }

    # ---------- ein Scan-Durchlauf ----------
    async def run_cycle(self):
        found = await self.market.discover()
        await self.add_candidates(found)
        due = self.db.due_candidates(self.s.get("recheck_interval_sec", default=300),
                                     self.s.get("max_watch_hours", default=24) * 3600)
        by_chain: dict[str, list] = {}
        for row in due:
            by_chain.setdefault(row["chain"], []).append(row)

        for chain, rows in by_chain.items():
            if chain not in CHAINS:
                continue
            pairs = await self.market.pairs_for_tokens(chain, [r["address"] for r in rows])
            for row in rows:
                addr = row["address"]
                pair = pairs.get(norm(chain, addr))
                try:
                    await self._process(chain, addr, row["source"], pair, row)
                except Exception:  # ein kaputter Coin darf den Bot nicht stoppen
                    log.exception("Fehler bei %s %s", chain, addr)
                    self.db.mark(chain, addr, "pending", "Fehler")

    async def add_candidates(self, found):
        new = sum(self.db.add_candidate(c, a, src) for c, a, src in found)
        if new:
            log.info("%d neue Kandidaten (%d gefunden)", new, len(found))
        return new

    async def _process(self, chain, addr, source, pair, row=None):
        if not pair:
            self.db.mark(chain, addr, "pending", "kein Paar")
            return
        m = pair_metrics(pair)
        self.db.con.execute("UPDATE tokens SET symbol=? WHERE chain=? AND address=?", (m["symbol"], chain, addr))
        fails = check_market(m, self.market_cfg(source))
        if fails:
            fatal = any(f.startswith(FATAL_MARKET) for f in fails)
            self.db.mark(chain, addr, "rejected" if fatal else "pending", "; ".join(fails))
            self.db.set_passed(chain, addr, None)
            return

        ev = await self.evaluate(chain, addr, source, pair=pair)
        sec = ev.security
        if not sec.passed:
            self.db.mark(chain, addr, "rejected" if sec.fatal else "pending", "; ".join(sec.hard_fails))
            self.db.set_passed(chain, addr, None)
            log.info("✗ %s %s: %s", m["symbol"], chain, "; ".join(sec.hard_fails)[:150])
            return
        if ev.market_fails or ev.total < self.s.get("min_score", default=65):
            self.db.mark(chain, addr, "pending", "; ".join(ev.market_fails) or f"Score {ev.total}")
            self.db.set_passed(chain, addr, None)
            log.info("~ %s %s Score %d zu niedrig", m["symbol"], chain, ev.total)
            return

        # ---- Bestätigung: Coin muss nach X Minuten immer noch alles erfüllen ----
        wait = self.s.get("confirm_minutes", default=0) * 60
        fast = ev.hype_score >= self.s.get("fast_lane_hype", default=101)
        passed_at = row["passed_at"] if row is not None and "passed_at" in row.keys() else None
        if wait and not fast:
            if not passed_at:
                self.db.set_passed(chain, addr, m["mcap"])
                self.db.mark(chain, addr, "pending", f"Bestätigung läuft (Score {ev.total})")
                log.info("… %s %s besteht – warte %d min zur Bestätigung", m["symbol"], chain, wait // 60)
                return
            if time.time() - passed_at < wait:
                self.db.mark(chain, addr, "pending", f"Bestätigung läuft (Score {ev.total})")
                return
            if row["passed_mcap"] and m["mcap"] < row["passed_mcap"] * 0.75:
                self.db.set_passed(chain, addr, None)
                self.db.mark(chain, addr, "pending", "In der Wartezeit >25 % gefallen")
                return

        self.db.mark(chain, addr, "alerted", f"Score {ev.total}")
        hype_info = f"X {ev.x.get('accounts', 0)} Acc · TG {ev.tgb.get('places', 0)} Kanäle" if (ev.x or ev.tgb) else ""
        self.db.add_call(chain, addr, m, ev.total, source, hype_info)
        log.info("✓ ALARM %s %s Score %d", m["symbol"], chain, ev.total)
        if not self.paused:
            await self.tg.alert(coin_message(ev, header=("⚡ HYPE-CALL – alle Kriterien erfüllt" if fast else
                                                         "🚀 NEUER COIN – alle Kriterien erfüllt")))

    # ---------- News-Trends -> Kandidaten ----------
    async def run_news(self):
        trends = await self.news.update_trends()
        coins = await self.news.coins_for_trends(trends, self.s.get("market", "max_age_hours", default=72))
        await self.add_candidates(coins)
        if coins:
            log.info("%d Coins zu News-Trends gefunden", len(coins))
        return trends
