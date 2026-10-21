"""Call-Tracker: merkt sich jeden gemeldeten Coin (MC beim Call) und verfolgt das Allzeithoch danach."""
import logging
import time
from html import escape

from .chains import CHAINS, norm
from .fmt import usd
from .market import pair_metrics

log = logging.getLogger("tracker")
MILESTONES = [2, 3, 5, 10, 20, 50, 100]
GT = "https://api.geckoterminal.com/api/v2"


def mult(a, b) -> float:
    return (a / b) if b else 0.0


def fx(x: float) -> str:
    return f"{x:.1f}x" if x < 10 else f"{x:.0f}x"


class CallTracker:
    def __init__(self, http, settings, db, market, tg):
        self.http, self.s, self.db, self.market, self.tg = http, settings, db, market, tg
        self._last_ohlcv: dict[str, float] = {}

    async def run_cycle(self):
        calls = self.db.active_calls(self.s.get("tracker", "track_days", default=14))
        by_chain: dict[str, list] = {}
        for c in calls:
            if not c["dead"]:
                by_chain.setdefault(c["chain"], []).append(c)
        for chain, rows in by_chain.items():
            if chain not in CHAINS:
                continue
            pairs = await self.market.pairs_for_tokens(chain, [r["address"] for r in rows])
            for r in rows:
                p = pairs.get(norm(chain, r["address"]))
                if not p:
                    continue
                m = pair_metrics(p)
                await self._update(r, m, p.get("pairAddress", ""))

    async def _ohlcv_high(self, chain, pool, since_ts) -> float | None:
        """Höchster Preis seit dem Call laut GeckoTerminal-Kerzen (fängt auch kurze Spitzen)."""
        net = CHAINS[chain]["gecko"]
        data = await self.http.get_json(
            f"{GT}/networks/{net}/pools/{pool}/ohlcv/minute?aggregate=5&limit=1000&currency=usd",
            headers={"Accept": "application/json;version=20230302"})
        try:
            rows = data["data"]["attributes"]["ohlcv_list"]
        except (TypeError, KeyError):
            return None
        highs = [float(r[2]) for r in rows if r[0] >= since_ts]
        return max(highs) if highs else None

    async def _update(self, r, m, pool):
        now = time.time()
        cur = m["mcap"]
        ath = max(r["ath_mcap"] or 0, cur)
        ath_ts = r["ath_ts"] if ath == (r["ath_mcap"] or 0) else now

        # alle 30 min genauere Spitze aus Kerzendaten holen
        key = f"{r['chain']}:{r['address']}"
        if pool and r["call_price"] and now - self._last_ohlcv.get(key, 0) > 1800:
            self._last_ohlcv[key] = now
            hi = await self._ohlcv_high(r["chain"], pool, r["call_ts"])
            if hi:
                cand = r["call_mcap"] * hi / r["call_price"]
                if cand > ath:
                    ath, ath_ts = cand, now

        low = min(r["min_mcap"] or r["call_mcap"], cur) if cur else (r["min_mcap"] or r["call_mcap"])
        fields = {"last_mcap": cur, "last_check": now, "ath_mcap": ath, "ath_ts": ath_ts, "min_mcap": low}
        x = mult(ath, r["call_mcap"])
        best = r["best_milestone"] or 1
        reached = [ms for ms in MILESTONES if x >= ms > best]
        if reached:
            ms = max(reached)
            fields["best_milestone"] = ms
            mins = (ath_ts - r["call_ts"]) / 60
            took = f"{mins:.0f} min" if mins < 120 else f"{mins / 60:.1f} h"
            tip = ("\n🎯 <b>Ziel 2x erreicht</b> – Tag-1-Auswertung: Gewinn (mind. die Hälfte) mitnehmen lohnt sich, "
                   "viele Coins fallen danach wieder." if best < 2 else "")
            await self.tg.alert(
                f"🚀 <b>${escape(r['symbol'])} hat {fx(x)} gemacht!</b>{tip}\n"
                f"📍 Call bei MC {usd(r['call_mcap'])} → 🏔 Hoch {usd(ath)} (nach {took})\n"
                f"Jetzt: MC {usd(cur)} ({fx(mult(cur, r['call_mcap']))})\n"
                f'<a href="{r["url"]}">Chart</a>')
        # Stop-Loss-Hinweis: 50 % unter Call, ohne vorher 2x gemacht zu haben
        if cur and r["call_mcap"] and cur <= r["call_mcap"] * 0.5 and not r["sl_notified"] and (fields.get("best_milestone") or best) < 2:
            fields["sl_notified"] = 1
            await self.tg.alert(
                f"🛑 <b>${escape(r['symbol'])} ist 50 % unter dem Call</b> (MC {usd(r['call_mcap'])} → {usd(cur)}).\n"
                "Stop-Loss-Regel: hier aussteigen begrenzt den Verlust – die meisten Coins in dieser Lage fielen weiter.")
        # tot / gerugged?
        if cur and r["call_mcap"] and cur < r["call_mcap"] * 0.15 and liq_dead(m):
            fields["dead"] = 1
            await self.tg.alert(
                f"💀 <b>${escape(r['symbol'])}</b> ist um über 85 % unter den Call gefallen "
                f"(MC {usd(cur)}, Liq {usd(m['liquidity'])}). Hoch war {usd(ath)} ({fx(x)}). Wird nicht mehr verfolgt.")
        self.db.update_call(r["chain"], r["address"], **fields)

    # ---------------- Übersichten ----------------
    def calls_text(self, limit=15) -> str:
        rows = self.db.recent_calls(limit)
        if not rows:
            return "Noch keine Calls. Sobald ein Coin alle Kriterien erfüllt, taucht er hier auf."
        lines = ["<b>📒 Letzte Calls</b>  (Call-MC → Hoch → Jetzt)", ""]
        for r in rows:
            x, now_x = mult(r["ath_mcap"], r["call_mcap"]), mult(r["last_mcap"], r["call_mcap"])
            icon = "💀" if r["dead"] else ("🟢" if x >= 2 else ("🟡" if x >= 1.3 else "🔴"))
            date = time.strftime("%d.%m. %H:%M", time.localtime(r["call_ts"]))
            lines.append(f'{icon} <a href="{r["url"]}">${escape(r["symbol"])}</a> ({CHAINS[r["chain"]]["name"]}, {date})\n'
                         f"   {usd(r['call_mcap'])} → 🏔 {usd(r['ath_mcap'])} <b>{fx(x)}</b> → jetzt {usd(r['last_mcap'])} ({fx(now_x)})")
        lines += ["", self.stats_line(rows)]
        return "\n".join(lines)

    def stats_line(self, rows) -> str:
        if not rows:
            return ""
        xs = [mult(r["ath_mcap"], r["call_mcap"]) for r in rows]
        wins = sum(1 for x in xs if x >= 2)
        best = max(rows, key=lambda r: mult(r["ath_mcap"], r["call_mcap"]))
        return (f"📊 {len(rows)} Calls · {wins} davon ≥2x ({wins / len(rows) * 100:.0f} %) · "
                f"Ø Hoch {sum(xs) / len(xs):.1f}x · Bester: ${escape(best['symbol'])} {fx(max(xs))}")

    def bilanz_text(self, stake: float = 10.0, fee: float = 0.05) -> str:
        rows = self.db.all_calls()
        if not rows:
            return "Noch keine Calls für eine Bilanz."
        n, inv = len(rows), len(rows) * stake

        def val(fn):
            return sum(stake * fn(r) * (1 - fee) for r in rows)

        hold = val(lambda r: mult(r["last_mcap"], r["call_mcap"]))
        tp = val(lambda r: 2.0 if mult(r["ath_mcap"], r["call_mcap"]) >= 2 else mult(r["last_mcap"], r["call_mcap"]))

        def tp_sl(r):
            if mult(r["ath_mcap"], r["call_mcap"]) >= 2:
                return 2.0
            low = mult(r["min_mcap"] or r["last_mcap"], r["call_mcap"])
            return 0.5 if low <= 0.5 else mult(r["last_mcap"], r["call_mcap"])
        tpsl = val(tp_sl)
        wins = sum(1 for r in rows if mult(r["ath_mcap"], r["call_mcap"]) >= 2)
        dead = sum(1 for r in rows if mult(r["last_mcap"], r["call_mcap"]) < 0.15)

        def line(name, v):
            return f"{name}: <b>{v:,.0f} €</b> ({(v - inv) / inv * 100:+.0f} %)"
        return "\n".join([
            f"<b>💶 Bilanz – {n} Calls à {stake:,.0f} € = {inv:,.0f} € Einsatz</b>", "",
            line("📦 Halten bis jetzt", hold),
            line("🎯 Bei 2x alles verkaufen", tp),
            line("🛡 2x verkaufen + Stop-Loss −50 %", tpsl), "",
            f"≥2x: {wins} · abgestürzt: {dead} · ca. 5 % Gebühren eingerechnet",
            "<i>Rechnung mit Call-Preisen – echte Käufe sind etwas teurer. Keine Anlageberatung.</i>"])

    async def daily_summary(self):
        last = self.db.kv_get("last_summary", 0)
        if time.time() - last < 24 * 3600:
            return
        self.db.kv_set("last_summary", time.time())
        rows = self.db.calls_since(time.time() - 24 * 3600)
        if not rows:
            return
        await self.tg.alert("<b>🗓 Tages-Bilanz (letzte 24 h)</b>\n\n" + self.calls_text_rows(rows)
                            + "\n\n" + self.bilanz_text())

    def calls_text_rows(self, rows) -> str:
        out = []
        for r in sorted(rows, key=lambda r: -mult(r["ath_mcap"], r["call_mcap"])):
            out.append(f"• ${escape(r['symbol'])}: {usd(r['call_mcap'])} → 🏔 {usd(r['ath_mcap'])} "
                       f"<b>{fx(mult(r['ath_mcap'], r['call_mcap']))}</b>")
        return "\n".join(out) + "\n\n" + self.stats_line(rows)


def liq_dead(m) -> bool:
    return m["liquidity"] < 2000 or m["vol_1h"] < 100
