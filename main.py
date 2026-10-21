"""MemeBot – startet alle Module.

    python main.py              # normaler Betrieb
    python main.py --once       # ein einzelner Scan-Durchlauf (Test)
    python main.py --check <adresse>   # einen Coin sofort prüfen (Test)
"""
import argparse
import asyncio
import os
import logging
import time
from html import escape

from bot.chains import CHAINS, is_evm_address, is_solana_address, norm
from bot.config import Settings
from bot.db import DB
from bot.fmt import coin_message
from bot.http import Http
from bot.market import Market
from bot.news import News
from bot.ai import AI
from bot.scanner import Scanner
from bot.security import SecurityChecker
from bot.telegram import Telegram, strip_html
from bot.wallets import WalletTracker
from bot.tracker import CallTracker
from bot.xhype import XHype
from bot.tghype import TgHype
from bot.config import ROOT

log = logging.getLogger("main")

HELP = """<b>🤖 MemeBot – Befehle</b>

/check &lt;adresse&gt; – Coin sofort komplett prüfen (Honeypot, Rug, Markt, Hype, KI)
/watch &lt;adresse&gt; [name] – Wallet beobachten (Solana oder 0x… für alle EVM-Chains)
/unwatch &lt;adresse|name&gt; – Wallet entfernen
/wallets – beobachtete Wallets · /smart – was kauft Smart Money gerade
/sol_rpc &lt;url&gt; – eigener Solana-Zugang (z. B. Helius, kostenlos) für schnelleres Wallet-Tracking
/calls – alle bisherigen Calls: MC beim Call, Allzeithoch danach, x-Faktor
/bilanz [einsatz] – was wäre aus deinem Geld geworden (3 Verkaufs-Strategien)
/ki_test – prüft die KI · /ki_key sk-ant-… – neuen Claude-Schlüssel setzen
/trends – aktuelle News-Trends
/hot – meisterwähnte Coins auf X & Telegram (letzte Stunde)
/hype &lt;adresse&gt; – X- und Telegram-Hype eines Coins messen
/x_feed – neueste Posts der beobachteten Promis
/status – Statistik

<b>Hype-Quellen einrichten</b>
/x_key &lt;schlüssel&gt; – twitterapi.io-Schlüssel · /x_test
/tg_api &lt;api_id&gt; &lt;api_hash&gt; → /tg_login &lt;+49…&gt; → /tg_code 1 2 3 4 5 (→ /tg_pw)
/tg_join @kanal – Zweit-Account tritt Kanal bei · /tg_status

<b>Einstellen &amp; Updates</b>
/get [bereich] – Einstellungen ansehen · /set name wert – ändern (z. B. /set min_score 70)
/set reset name – zurück auf Standard · /update – neueste Version von GitHub laden
/pause · /resume – Coin-Alarme an/aus (Wallet-Alarme laufen weiter)
/kanal_aus – Alarme wieder nur privat (Kanal: Bot als Admin hinzufügen)

Du kannst mir auch einfach eine Coin-Adresse schicken."""





SECRET_CMDS = ("/sol_rpc", "/ki_key", "/x_key", "/tg_api", "/tg_login", "/tg_code", "/tg_pw")


class App:
    def __init__(self):
        self.s = Settings()
        self.db = DB(self.s.db_path)
        if not self.s.telegram_chat_id:
            self.s.telegram_chat_id = self.db.kv_get("owner_chat", "") or ""
        self.http = Http()
        self.tg = Telegram(self.http, self.s)
        if self.db.kv_get("ki_key"):
            self.s.anthropic_key = self.db.kv_get("ki_key")
        ac = self.db.kv_get("alert_chat", "")
        self.tg.alert_chat = "" if ac == "off" else (ac or os.getenv("ALERT_CHAT_ID", "").strip())
        self.market = Market(self.http, self.s)
        self.ai = AI(self.http, self.s, self.db)
        self.security = SecurityChecker(self.http, self.s)
        self.s.overrides.update(self.db.kv_get("overrides", {}) or {})
        self.news = News(self.http, self.s, self.db, self.ai, self.market)
        self.xhype = XHype(self.http, self.s, self.db, self.ai, self.market)
        self.tghype = TgHype(self.s, self.db, self.market, self.tg)
        self.scanner = Scanner(self.s, self.db, self.market, self.security, self.news, self.ai, self.tg,
                               self.xhype, self.tghype)
        self.wallets = WalletTracker(self.http, self.s, self.db, self.market, self.security, self.tg, self.scanner)
        if self.db.kv_get("sol_rpc"):
            self.s.solana_rpc = self.db.kv_get("sol_rpc")
        n = self.wallets.import_list()
        if n:
            log.info("%d Wallets aus der Liste übernommen", n)
        self.tracker = CallTracker(self.http, self.s, self.db, self.market, self.tg)
        self.started = time.time()

    # ------------------------- Schleifen -------------------------
    async def loop(self, name, coro_fn, interval):
        while True:
            t0 = time.time()
            try:
                await coro_fn()
            except Exception:
                log.exception("Fehler in %s", name)
            await asyncio.sleep(max(5, interval - (time.time() - t0)))

    async def news_job(self):
        if not self.s.get("news", "enabled", default=True):
            return
        trends = await self.scanner.run_news()
        every_h = self.s.get("news", "digest_every_hours", default=6) or 0
        last = self.db.kv_get("last_digest", 0)
        if every_h and trends and time.time() - last > every_h * 3600:
            self.db.kv_set("last_digest", time.time())
            await self.tg.alert(self.trends_text(trends))

    async def x_job(self):
        found = await self.xhype.kol_cycle()
        await self.scanner.add_candidates(found)
        for c, a, _ in found:
            self.db.bump(c, a)

    async def x_loop(self):
        while True:
            t0 = time.time()
            try:
                if self.xhype.enabled:
                    await self.x_job()
            except Exception:
                log.exception("Fehler in X")
            await asyncio.sleep(max(30, self.s.get("x", "interval_sec", default=180) - (time.time() - t0)))

    async def housekeeping(self):
        self.db.cleanup()

    def trends_text(self, trends=None) -> str:
        trends = trends or self.db.kv_get("trends", [])
        if not trends:
            return "Noch keine Trends gesammelt – kommt in ein paar Minuten."
        lines = ["<b>📰 Trend-Radar (mögliche Meme-Themen)</b>", ""]
        for t in trends[:12]:
            lines.append(f"• <b>{escape(t['keyword'])}</b> – {escape(t.get('why', ''))}\n"
                         f"  <i>{escape(t.get('headline', ''))[:120]}</i>")
        lines.append("\nNeue Coins zu diesen Themen werden automatisch geprüft.")
        return "\n".join(lines)

    # ------------------------- Telegram-Befehle -------------------------
    async def telegram_loop(self):
        await self.tg.set_commands()
        while True:
            try:
                for u in await self.tg.updates():
                    if u.get("my_chat_member"):
                        await self.on_membership(u["my_chat_member"])
                        continue
                    msg = u.get("message") or {}
                    chat = str((msg.get("chat") or {}).get("id", ""))
                    text = (msg.get("text") or "").strip()
                    if not text:
                        continue
                    if not self.s.telegram_chat_id:
                        # Wer als Erstes /start schreibt, wird Besitzer des Bots (wird gespeichert)
                        if text.lower().startswith("/start"):
                            self.s.telegram_chat_id = chat
                            self.db.kv_set("owner_chat", chat)
                            await self.tg.send("✅ <b>Du bist jetzt als Besitzer eingetragen.</b>\n"
                                               "Nur du bekommst Alarme und kannst Befehle nutzen.\n\n" + HELP)
                        continue
                    if chat != self.s.telegram_chat_id:
                        continue  # fremde Nutzer ignorieren
                    if text.lower().split("@")[0].split(" ")[0] in SECRET_CMDS:
                        await self.tg.delete(chat, msg.get("message_id"))  # Schlüssel/Codes nicht im Chat lassen
                    asyncio.create_task(self.handle(text))
            except Exception:
                log.exception("Telegram-Fehler")
                await asyncio.sleep(5)
            if not self.tg.active:
                await asyncio.sleep(3600)

    def ai_state(self) -> str:
        if not self.ai.enabled:
            return "aus"
        st = self.db.kv_get("ai_status") or {}
        if not st:
            return "an (noch nicht benutzt)"
        return "✅ ok" if st.get("ok") else f"❌ Fehler {st.get('code')} – /ki_test"

    async def on_membership(self, upd: dict):
        """Bot wurde in einem Kanal/Gruppe als Admin hinzugefügt oder entfernt."""
        chat = upd.get("chat") or {}
        cid, title = str(chat.get("id", "")), chat.get("title", "Kanal")
        who = str((upd.get("from") or {}).get("id", ""))
        status = (upd.get("new_chat_member") or {}).get("status")
        if chat.get("type") not in ("channel", "supergroup", "group"):
            return
        if status == "administrator" and who == self.s.telegram_chat_id:
            self.tg.alert_chat = cid
            self.db.kv_set("alert_chat", cid)
            await self.tg.send(f"✅ <b>Kanal „{escape(title)}“ verbunden.</b>\n"
                               "Alle Coin-, Wallet- und Trend-Alarme kommen ab jetzt dorthin.\n"
                               "Befehle schickst du weiter hier im privaten Chat.\n"
                               "Mit /kanal_aus gehen die Alarme wieder nur an dich.")
            await self.tg.send("🤖 <b>MemeBot ist jetzt in diesem Kanal aktiv.</b>\n"
                               "Hier erscheinen neue Coins, die alle Sicherheits-Checks bestehen, "
                               "Käufe beobachteter Wallets und News-Trends.", chat_id=cid)
        elif status in ("left", "kicked", "member", "restricted") and cid == self.tg.alert_chat:
            self.tg.alert_chat = ""
            self.db.kv_set("alert_chat", "")
            await self.tg.send(f"ℹ️ Der Bot ist nicht mehr Admin in „{escape(title)}“. "
                               "Alarme kommen wieder privat zu dir.")

    async def handle(self, text: str):
        parts = text.split()
        cmd = parts[0].lower().split("@")[0]
        args = parts[1:]
        if not cmd.startswith("/") and (is_evm_address(parts[0]) or is_solana_address(parts[0])):
            cmd, args = "/check", parts

        if cmd in ("/start", "/hilfe", "/help"):
            await self.tg.send(HELP)
        elif cmd == "/check":
            if not args:
                return await self.tg.send("Bitte so: /check &lt;token-adresse&gt;")
            await self.tg.send("🔍 Prüfe… (kann bis zu 1 Minute dauern)")
            await self.tg.send(await self.check_text(args[0]))
        elif cmd == "/watch":
            if not args:
                return await self.tg.send("Bitte so: /watch &lt;wallet-adresse&gt; [name]")
            addr = args[0]
            label = " ".join(args[1:]) or f"{addr[:4]}…{addr[-4:]}"
            if is_evm_address(addr):
                self.db.add_wallet("evm", addr.lower(), label)
                chains = ", ".join(CHAINS[c]["name"] for c in self.s.chains if c != "solana")
                await self.tg.send(f"👀 Beobachte <b>{escape(label)}</b> auf {chains}.\n"
                                   "Ab jetzt melde ich jeden neuen Kauf/Verkauf.")
            elif is_solana_address(addr):
                self.db.add_wallet("solana", addr, label)
                await self.tg.send(f"👀 Beobachte <b>{escape(label)}</b> auf Solana.\n"
                                   "Ab jetzt melde ich jeden neuen Kauf/Verkauf.")
            else:
                await self.tg.send("❌ Das ist keine gültige Wallet-Adresse.")
        elif cmd == "/unwatch":
            n = self.db.remove_wallet(" ".join(args)) if args else 0
            await self.tg.send("🗑 Entfernt." if n else "Nicht gefunden – siehe /wallets")
        elif cmd == "/wallets":
            ws = self.db.wallets()
            if not ws:
                return await self.tg.send("Noch keine Wallets. Füge eine hinzu mit /watch &lt;adresse&gt; [name]")
            people: dict[str, list] = {}
            for w in ws:
                people.setdefault(w["label"], []).append("SOL" if w["chain"] == "solana" else "EVM")
            lines = [f"<b>👛 {len(people)} beobachtete Wallets</b> ({len(ws)} Adressen)", ""] + [
                f"• {escape(k)} ({'+'.join(v)})" for k, v in people.items()]
            lines.append("\nEntfernen: /unwatch &lt;name&gt; · Smart-Money-Coins: /smart")
            await self.tg.send("\n".join(lines))
        elif cmd == "/smart":
            await self.tg.send(self.smart_text())
        elif cmd == "/sol_rpc":
            if not args or not args[0].startswith("http"):
                return await self.tg.send("So: /sol_rpc https://mainnet.helius-rpc.com/?api-key=… (Nachricht wird gelöscht)")
            self.s.solana_rpc = args[0]
            self.db.kv_set("sol_rpc", args[0])
            await self.tg.send("✅ Eigener Solana-Zugang gespeichert – Wallets werden jetzt schneller und zuverlässiger geprüft.")
        elif cmd == "/ki_key":
            if not args or not args[0].startswith("sk-ant-"):
                return await self.tg.send("So: /ki_key sk-ant-… (Nachricht wird danach automatisch gelöscht)")
            self.s.anthropic_key = args[0].strip()
            self.db.kv_set("ki_key", self.s.anthropic_key)
            self.db.kv_set("ai_auth", None)
            await self.tg.send("🔑 Neuer Claude-Schlüssel gespeichert (deine Nachricht wurde gelöscht). Teste…")
            await self.tg.send(await self.ai.selftest())
        elif cmd == "/ki_test":
            await self.tg.send("🧪 Teste KI…")
            await self.tg.send(await self.ai.selftest())
        elif cmd == "/bilanz":
            stake = float(args[0].replace(",", ".")) if args and args[0].replace(",", ".").replace(".", "", 1).isdigit() else 10.0
            await self.tg.send(self.tracker.bilanz_text(stake))
        elif cmd == "/calls":
            await self.tg.send(self.tracker.calls_text())
        elif cmd == "/trends":
            await self.tg.send(self.trends_text())
        elif cmd == "/status":
            st = self.db.stats()
            up = (time.time() - self.started) / 3600
            await self.tg.send(
                f"<b>📊 Status</b>\nLäuft seit {up:.1f} h · Alarme {'⏸ pausiert' if self.scanner.paused else '▶️ aktiv'}\n"
                f"Chains: {', '.join(CHAINS[c]['name'] for c in self.s.chains)}\n"
                f"Coins in Beobachtung: {st.get('pending', 0)}\n"
                f"Gemeldet: {st.get('alerted', 0)} · Aussortiert: {st.get('rejected', 0)}\n"
                f"Alarme an: {'Kanal' if self.tg.alert_chat else 'dich privat'}\n"
                f"Wallets: {len(self.db.wallets())} · KI: {self.ai_state()} "
                f"({self.db.ai_calls_last_hour('coin')} Coin-Analysen letzte Stunde)\n"
                f"🐦 X: {self.x_state()}\n✈️ Telegram-Mitlesen: {self.tghype.status()}\n"
                f"Version: {self.version()}")
        elif cmd == "/x_key":
            if not args:
                return await self.tg.send("So: /x_key &lt;dein twitterapi.io-Schlüssel&gt; (Nachricht wird gelöscht)")
            self.db.kv_set("x_key", args[0].strip())
            await self.tg.send("🔑 X-Schlüssel gespeichert (deine Nachricht wurde gelöscht). Teste…")
            await self.tg.send(await self.x_test())
        elif cmd == "/x_test":
            await self.tg.send(await self.x_test())
        elif cmd == "/x_feed":
            feed = self.db.kv_get("x_feed", [])
            if not feed:
                return await self.tg.send("Noch keine Posts gesammelt (X-Schlüssel gesetzt? /x_test)")
            await self.tg.send("<b>🐦 Neueste Promi-/KOL-Posts</b>\n\n" + "\n\n".join(
                f"<b>@{escape(p['user'])}</b> ({time.strftime('%H:%M', time.localtime(p['ts']))}): {escape(p['text'])}"
                for p in reversed(feed[-12:])))
        elif cmd == "/hype":
            if not args:
                return await self.tg.send("So: /hype &lt;coin-adresse&gt;")
            await self.tg.send(await self.hype_text(args[0]))
        elif cmd == "/hot":
            await self.tg.send(await self.hot_text())
        elif cmd == "/tg_api":
            if len(args) != 2 or not args[0].isdigit():
                return await self.tg.send("So: /tg_api &lt;api_id&gt; &lt;api_hash&gt; (von my.telegram.org)")
            self.db.kv_set("tg_api", {"id": args[0], "hash": args[1]})
            await self.tg.send("✅ API-Daten gespeichert (Nachricht gelöscht). Jetzt: /tg_login &lt;Telefonnummer des Zweit-Accounts, z. B. +4917…&gt;")
        elif cmd == "/tg_login":
            if not args:
                return await self.tg.send("So: /tg_login +49…")
            await self.tg.send(await self.safe(self.tghype.login_start("".join(args))))
        elif cmd == "/tg_code":
            await self.tg.send(await self.safe(self.tghype.login_code("".join(args))))
        elif cmd == "/tg_pw":
            await self.tg.send(await self.safe(self.tghype.login_pw(" ".join(args))))
        elif cmd == "/tg_join":
            if not args:
                return await self.tg.send("So: /tg_join @kanalname  oder  /tg_join https://t.me/+einladung")
            await self.tg.send(await self.tghype.join(args[0]))
        elif cmd == "/tg_logout":
            await self.tghype.logout()
            await self.tg.send("👋 Telegram-Mitlesen abgemeldet.")
        elif cmd == "/tg_status":
            await self.tg.send(f"✈️ Telegram-Mitlesen: {self.tghype.status()}")
        elif cmd == "/set":
            await self.tg.send(self.set_cmd(args))
        elif cmd == "/get":
            await self.tg.send(self.get_cmd(args))
        elif cmd == "/update":
            await self.update_cmd()
        elif cmd == "/kanal_aus":
            self.tg.alert_chat = ""
            self.db.kv_set("alert_chat", "off")
            await self.tg.send("📴 Kanal getrennt – Alarme kommen wieder nur privat zu dir.")
        elif cmd == "/pause":
            self.scanner.paused = True
            await self.tg.send("⏸ Coin-Alarme pausiert. /resume zum Fortsetzen.")
        elif cmd == "/resume":
            self.scanner.paused = False
            await self.tg.send("▶️ Coin-Alarme wieder aktiv.")
        else:
            await self.tg.send("Unbekannter Befehl. /hilfe")

    def smart_text(self) -> str:
        hours = self.s.get("wallets", "cluster_hours", default=12)
        rows = self.db.con.execute(
            "SELECT token, chain, COUNT(DISTINCT label) n FROM wallet_trades WHERE dir='in' AND ts>? "
            "GROUP BY token ORDER BY n DESC LIMIT 12", (time.time() - hours * 3600,)).fetchall()
        if not rows:
            return f"Noch keine Smart-Money-Käufe in den letzten {hours} h."
        lines = [f"<b>🐋 Smart Money – letzte {hours} h</b>", ""]
        for r in rows:
            b = self.db.wallet_buyers(r["token"], hours)
            hold = [x for x in b if not x["exited"]]
            tok = self.db.get_token(r["chain"], r["token"])
            sym = (tok["symbol"] if tok and tok["symbol"] else r["token"][:6] + "…")
            lines.append(f"• <b>{escape(sym)}</b> – {len(hold)} drin, {len(b) - len(hold)} raus: "
                         f"{escape(', '.join(x['label'] for x in hold[:5]))}\n  <code>{r['token']}</code>")
        return "\n".join(lines)

    async def safe(self, coro):
        try:
            return await coro
        except Exception as e:
            log.exception("Befehl")
            return f"❌ Fehler: {escape(type(e).__name__)}: {escape(str(e))[:200]}"

    def x_state(self) -> str:
        if not self.xhype.key:
            return "aus (/x_key)"
        st = self.db.kv_get("x_status") or {}
        if not st:
            return "an (noch nicht benutzt)"
        return "✅ ok" if st.get("ok") else f"❌ Fehler {st.get('code')} – /x_test"

    async def x_test(self) -> str:
        if not self.xhype.key:
            return "❌ Kein X-Schlüssel. Bei twitterapi.io anmelden, Schlüssel kopieren und /x_key &lt;schlüssel&gt; schicken."
        res = await self.xhype.search("from:elonmusk", pages=1)
        if res is None:
            st = self.db.kv_get("x_status") or {}
            return f"❌ X-Fehler {st.get('code')}: <code>{escape(str(st.get('msg', '')))[:200]}</code>"
        last = f"\nLetzter Elon-Post: <i>{escape(res[0]['text'][:150])}</i>" if res else ""
        return f"✅ X funktioniert ({len(res)} Posts gelesen).{last}"

    async def hype_text(self, address: str) -> str:
        found = await self.market.find_token(address)
        if not found:
            return "❌ Kein handelbares Paar für diese Adresse gefunden."
        chain, pair = found
        addr = norm(chain, address)
        sym = (pair.get("baseToken") or {}).get("symbol", "?")
        xb = await self.xhype.buzz(sym, addr) if self.xhype.enabled else {}
        tb = self.tghype.buzz(addr)
        lines = [f"<b>📣 Hype-Check ${escape(sym)}</b>", ""]
        if xb:
            lines.append(f"🐦 X letzte Stunde: <b>{xb['tweets']}{'+' if xb.get('full') else ''} Posts</b> von "
                         f"{xb['accounts']} Accounts · X-Hype {xb['score']}/100")
            if xb.get("big"):
                lines.append("   Große Accounts: " + escape(", ".join(xb["big"])))
        else:
            lines.append("🐦 X: " + self.x_state())
        lines.append(f"✈️ Telegram letzte Stunde: <b>{tb['places']} Kanäle</b>, {tb['n']} Erwähnungen"
                     + ("" if self.tghype.running else f" ({self.tghype.status()})"))
        return "\n".join(lines)

    async def hot_text(self) -> str:
        rows = self.db.top_mentions(60, 10)
        if not rows:
            return "Noch keine Erwähnungen in der letzten Stunde (X/Telegram eingerichtet? /status)"
        lines = ["<b>🔥 Meisterwähnt – letzte Stunde</b>", ""]
        for r in rows:
            tok = self.db.con.execute("SELECT symbol, status, chain FROM tokens WHERE address=?", (r["addr"],)).fetchone()
            sym = (tok["symbol"] if tok and tok["symbol"] else "") or r["addr"][:6] + "…"
            st = {"alerted": "✅ gecallt", "rejected": "⛔ aussortiert", "pending": "⏳ in Prüfung"}.get(
                tok["status"] if tok else "", "")
            lines.append(f"• <b>{escape(sym)}</b> – {r['places']} Orte, {r['n']} Erwähnungen {st}\n  <code>{r['addr']}</code>")
        return "\n".join(lines)

    # ---------- /set /get /update ----------
    def set_cmd(self, args) -> str:
        if len(args) >= 2 and args[0].lower() == "reset":
            self.s.overrides.pop(args[1], None)
            self.db.kv_set("overrides", self.s.overrides)
            return f"↩️ {escape(args[1])} wieder auf Standard."
        if len(args) < 2:
            return "So: /set name wert – z. B. /set min_score 70 oder /set market.max_market_cap_usd 300000\nAlle Namen: /get"
        try:
            val = self.s.set_value(args[0], " ".join(args[1:]))
        except ValueError as e:
            return f"❌ {escape(str(e))} – siehe /get"
        self.db.kv_set("overrides", self.s.overrides)
        return f"✅ <b>{escape(args[0])}</b> = {escape(str(val))[:300]}\n(gilt sofort, bleibt auch nach Neustart)"

    def get_cmd(self, args) -> str:
        def flat(d, pre=""):
            for k, v in d.items():
                if isinstance(v, dict):
                    yield from flat(v, f"{pre}{k}.")
                elif not isinstance(v, list) or len(v) <= 6:
                    yield f"{pre}{k}", v
                else:
                    yield f"{pre}{k}", f"[{len(v)} Einträge]"
        want = args[0] if args else ""
        lines = ["<b>⚙️ Einstellungen</b> (ändern mit /set name wert)", ""]
        for k, v in flat(self.s.cfg):
            if k == "news.feeds" or (want and not k.startswith(want)):
                continue
            cur = self.s.overrides.get(k, v)
            mark = " ✏️" if k in self.s.overrides else ""
            lines.append(f"<code>{escape(k)}</code> = {escape(str(cur))[:80]}{mark}")
        return "\n".join(lines)

    def version(self) -> str:
        try:
            head = (ROOT / ".git" / "HEAD").read_text().strip()
            ref = ROOT / ".git" / head.split(" ", 1)[1] if head.startswith("ref:") else None
            return (ref.read_text().strip() if ref and ref.exists() else head)[:7]
        except Exception:
            return "?"

    async def update_cmd(self):
        if not (ROOT / ".git").exists():
            return await self.tg.send("❌ Diese Installation kommt nicht von GitHub – Update geht nur per Neuinstallation.")
        await self.tg.send(f"⬇️ Lade neueste Version (aktuell {self.version()})…")
        p = await asyncio.create_subprocess_exec("git", "-C", str(ROOT), "pull", "--ff-only",
                                                 stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        out = (await p.communicate())[0].decode(errors="replace")[-600:]
        if p.returncode != 0:
            return await self.tg.send(f"❌ Update fehlgeschlagen:\n<code>{escape(out)}</code>")
        if "Already up to date" in out or "Bereits aktuell" in out:
            return await self.tg.send("✅ Schon die neueste Version.")
        await self.tg.send(f"✅ Neue Version {self.version()} geladen – starte neu… (dauert ~20 Sek.)")
        await asyncio.sleep(2)
        os._exit(0)  # systemd startet den Bot automatisch neu

    async def check_text(self, address: str) -> str:
        found = await self.market.find_token(address)
        if not found:
            return "❌ Kein handelbares Paar für diese Adresse gefunden."
        chain, pair = found
        addr = norm(chain, address)
        ev = await self.scanner.evaluate(chain, addr, "Manuelle Prüfung", pair=pair, force_ai=True)
        if ev.criteria_ok and ev.total >= self.s.get("min_score", default=65):
            header = "✅ ERFÜLLT ALLE KRITERIEN"
        elif ev.security and ev.security.fatal:
            header = "⛔ FINGER WEG"
        else:
            header = "⚠️ ERFÜLLT NICHT ALLE KRITERIEN"
        return coin_message(ev, header=header, is_call=False)

    # ------------------------- Start -------------------------
    async def run(self):
        log.info("MemeBot startet – Chains: %s | Telegram: %s | KI: %s",
                 ", ".join(self.s.chains), "an" if self.tg.active else "AUS (Konsole)",
                 "an" if self.ai.enabled else "aus")
        await self.tg.send("🤖 <b>MemeBot gestartet</b>\nIch beobachte jetzt "
                           f"{', '.join(CHAINS[c]['name'] for c in self.s.chains)}.\n/hilfe für Befehle.")
        tasks = [
            self.loop("scan", self.scanner.run_cycle, self.s.get("scan_interval_sec", default=60)),
            self.loop("wallets-sol", self.wallets.run_solana, self.s.get("wallets", "poll_interval_sec", default=60)),
            self.loop("wallets-evm", self.wallets.run_evm, self.s.get("wallets", "evm_interval_sec", default=300)),
            self.loop("news", self.news_job, self.s.get("news", "interval_min", default=20) * 60),
            self.loop("cleanup", self.housekeeping, 6 * 3600),
            self.loop("tracker", self.tracker.run_cycle, self.s.get("tracker", "interval_sec", default=90)),
            self.loop("summary", self.tracker.daily_summary, 600),
            self.x_loop(),
            self.tghype.run(),
            self.telegram_loop(),
        ]
        await asyncio.gather(*tasks)


async def amain():
    p = argparse.ArgumentParser()
    p.add_argument("--once", action="store_true", help="ein Scan-Durchlauf, dann Ende")
    p.add_argument("--check", metavar="ADRESSE", help="einen Coin prüfen und Ergebnis ausgeben")
    a = p.parse_args()
    app = App()
    try:
        if a.check:
            print(strip_html(await app.check_text(a.check)))
        elif a.once:
            await app.news_job()
            await app.scanner.run_cycle()
            print("Status:", app.db.stats())
        else:
            await app.run()
    finally:
        await app.http.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)-9s %(message)s",
                        datefmt="%H:%M:%S")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    asyncio.run(amain())
