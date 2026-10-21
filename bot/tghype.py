"""Telegram-Hype: liest mit einem ZWEIT-Account (Telethon) Alpha-/Call-Kanäle mit.

Jede Coin-Adresse, die in einem Kanal auftaucht, wird gezählt (wie viele Kanäle, wie oft)
und als Kandidat in den Scanner gegeben. Der Account wird per Telegram-Befehl eingerichtet:
/tg_api <id> <hash>  ->  /tg_login <+49…>  ->  /tg_code 1 2 3 4 5  ->  (/tg_pw <passwort>)
"""
import asyncio
import logging
import re
import time

from .chains import norm

log = logging.getLogger("tghype")
ADDR_RE = re.compile(r"(?<![A-Za-z0-9])(0x[a-fA-F0-9]{40}|[1-9A-HJ-NP-Za-km-z]{32,44})(?![A-Za-z0-9])")

try:
    from telethon import TelegramClient, events, errors
    from telethon.sessions import StringSession
    from telethon.tl.functions.channels import JoinChannelRequest
    from telethon.tl.functions.messages import ImportChatInviteRequest
except ImportError:  # Telethon nicht installiert -> Modul bleibt aus
    TelegramClient = None


class TgHype:
    def __init__(self, settings, db, market, tg):
        self.s, self.db, self.market, self.tg = settings, db, market, tg
        self.client = None
        self.phone = None
        self.code_hash = None
        self.chain_cache: dict[str, str | None] = {}
        self.seen_msgs = 0

    @property
    def cfg(self):
        return self.s.section("telegram_hype")

    @property
    def api(self):
        a = self.db.kv_get("tg_api") or {}
        return (a.get("id") or self.s.tg_api_id, a.get("hash") or self.s.tg_api_hash)

    @property
    def available(self):
        return TelegramClient is not None

    @property
    def running(self):
        return bool(self.client and self.client.is_connected() and self.db.kv_get("tg_session"))

    def status(self) -> str:
        if not self.available:
            return "❌ Telethon fehlt auf dem Server"
        if not all(self.api):
            return "aus (erst /tg_api)"
        if not self.db.kv_get("tg_session"):
            return "nicht eingeloggt (/tg_login)"
        return f"✅ liest mit ({self.seen_msgs} Nachrichten seit Start)" if self.running else "⏳ verbindet…"

    def _new_client(self, session=""):
        api_id, api_hash = self.api
        return TelegramClient(StringSession(session), int(api_id), api_hash,
                              device_model="MemeBot", system_version="Linux", app_version="1.0")

    # ---------------- Login per Bot-Befehlen ----------------
    async def login_start(self, phone: str) -> str:
        if not self.available:
            return "❌ Telethon ist auf dem Server nicht installiert."
        if not all(self.api):
            return "Zuerst /tg_api &lt;api_id&gt; &lt;api_hash&gt; (von my.telegram.org)."
        if self.client:
            await self.client.disconnect()
        self.client = self._new_client()
        await self.client.connect()
        self.phone = phone
        sent = await self.client.send_code_request(phone)
        self.code_hash = sent.phone_code_hash
        return ("📲 Telegram hat dem <b>Zweit-Account</b> einen Code geschickt.\n"
                "Schick ihn mir <b>mit Leerzeichen</b> zwischen den Ziffern, sonst blockt Telegram den Login:\n"
                "<code>/tg_code 1 2 3 4 5</code>")

    async def login_code(self, code: str) -> str:
        if not self.client or not self.phone:
            return "Zuerst /tg_login &lt;telefonnummer&gt;"
        try:
            await self.client.sign_in(self.phone, code.replace(" ", ""), phone_code_hash=self.code_hash)
        except errors.SessionPasswordNeededError:
            return "🔐 Der Account hat ein Zwei-Schritt-Passwort. Schick: /tg_pw &lt;passwort&gt;"
        except Exception as e:
            return f"❌ Login fehlgeschlagen: {type(e).__name__}. Nochmal mit /tg_login versuchen."
        return await self._logged_in()

    async def login_pw(self, pw: str) -> str:
        if not self.client:
            return "Zuerst /tg_login &lt;telefonnummer&gt;"
        try:
            await self.client.sign_in(password=pw)
        except Exception as e:
            return f"❌ Passwort falsch oder Fehler: {type(e).__name__}"
        return await self._logged_in()

    async def _logged_in(self) -> str:
        self.db.kv_set("tg_session", self.client.session.save())
        me = await self.client.get_me()
        self._attach(self.client)
        joined = await self.join_defaults()
        return (f"✅ Eingeloggt als <b>{(me.first_name or '')}</b>. Ich lese jetzt alle Kanäle/Gruppen dieses Accounts mit."
                + (f"\nAutomatisch beigetreten: {joined}" if joined else "")
                + "\nTipp: Tritt mit dem Zweit-Account guten Call-/Alpha-Kanälen bei oder nutze /tg_join @kanal")

    async def logout(self):
        self.db.kv_set("tg_session", None)
        if self.client:
            try:
                await self.client.log_out()
            except Exception:
                pass
            self.client = None

    async def join(self, target: str) -> str:
        if not self.running:
            return "Telegram-Mitlesen ist nicht eingeloggt."
        t = target.strip()
        try:
            if "/+" in t or "joinchat/" in t:
                await self.client(ImportChatInviteRequest(t.rsplit("/", 1)[-1].lstrip("+")))
            else:
                await self.client(JoinChannelRequest(t.split("t.me/")[-1].lstrip("@")))
            return f"✅ Beigetreten: {t}"
        except Exception as e:
            return f"❌ Konnte {t} nicht beitreten ({type(e).__name__})"

    async def join_defaults(self) -> str:
        done = []
        for ch in self.cfg.get("channels") or []:
            r = await self.join(ch)
            if r.startswith("✅"):
                done.append(ch)
            await asyncio.sleep(3)  # nicht zu schnell (Telegram-Flood-Schutz)
        return ", ".join(done)

    # ---------------- Mitlesen ----------------
    def _attach(self, client):
        @client.on(events.NewMessage(incoming=True))
        async def _on_msg(event):
            try:
                await self.on_message(event)
            except Exception:
                log.exception("TG-Nachricht")

    async def on_message(self, event):
        if event.is_private:
            return
        text = event.raw_text or ""
        self.seen_msgs += 1
        addrs = set(ADDR_RE.findall(text))
        if not addrs:
            return
        chat = await event.get_chat()
        title = getattr(chat, "title", None) or getattr(chat, "username", None) or str(event.chat_id)
        await self.record(title, addrs)

    async def record(self, place: str, addrs) -> list[tuple[str, str, str]]:
        found = []
        for a in list(addrs)[:5]:
            chain = await self._chain_for(a)
            if not chain:
                continue
            addr = norm(chain, a)
            self.db.add_mention(addr, "tg", place, 1)
            if self.db.add_candidate(chain, addr, f"Telegram: {place}"):
                log.info("TG-Kandidat %s aus %s", addr, place)
            else:
                self.db.bump(chain, addr)  # schon bekannt -> bald neu prüfen (Hype steigt)
            found.append((chain, addr, place))
        return found

    async def _chain_for(self, a: str) -> str | None:
        if a in self.chain_cache:
            return self.chain_cache[a]
        res = await self.market.find_token(a)
        ch = res[0] if res and res[0] in self.s.chains else None
        if len(self.chain_cache) > 5000:
            self.chain_cache.clear()
        self.chain_cache[a] = ch
        return ch

    def buzz(self, addr: str, minutes=60) -> dict:
        st = self.db.mention_stats(addr, minutes, "tg")
        score = min(100, st["places"] * 15 + st["n"] * 2)
        return {**st, "score": score}

    async def run(self):
        """Dauerschleife: verbindet den Zweit-Account, sobald eine Sitzung gespeichert ist."""
        while True:
            sess = self.db.kv_get("tg_session")
            if not (self.available and sess and all(self.api)):
                await asyncio.sleep(20)
                continue
            try:
                if not self.client or not self.client.is_connected():
                    self.client = self._new_client(sess)
                    await self.client.connect()
                    if not await self.client.is_user_authorized():
                        self.db.kv_set("tg_session", None)
                        await self.tg.send("⚠️ Telegram-Mitlesen wurde abgemeldet. Bitte neu: /tg_login &lt;nummer&gt;")
                        continue
                    self._attach(self.client)
                    log.info("Telegram-Mitlesen verbunden")
                await self.client.run_until_disconnected()
            except Exception:
                log.exception("Telegram-Mitlesen")
            await asyncio.sleep(30)
