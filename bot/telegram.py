"""Minimaler Telegram-Client (Senden + Befehle empfangen per Long-Polling)."""
import logging
import re

log = logging.getLogger("telegram")


def strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text)


class Telegram:
    def __init__(self, http, settings):
        self.http = http
        self.s = settings
        self.base = f"https://api.telegram.org/bot{settings.telegram_token}"
        self.offset = 0

    @property
    def active(self) -> bool:
        return bool(self.s.telegram_token) and not self.s.dry_run

    async def send(self, text: str, chat_id: str | None = None, preview=False):
        chat_id = chat_id or self.s.telegram_chat_id
        if not self.active or not chat_id:
            print("\n" + "=" * 60 + "\n" + strip_html(text) + "\n" + "=" * 60, flush=True)
            return
        for chunk in _chunks(text, 4000):
            r = await self.http.request("POST", f"{self.base}/sendMessage", json={
                "chat_id": chat_id, "text": chunk, "parse_mode": "HTML",
                "disable_web_page_preview": not preview,
            })
            if r is None or r.status_code != 200:
                log.error("Senden fehlgeschlagen: %s", r.text[:300] if r is not None else "keine Antwort")
                # Fallback ohne HTML, falls Formatierung kaputt war
                await self.http.request("POST", f"{self.base}/sendMessage",
                                        json={"chat_id": chat_id, "text": strip_html(chunk)})

    async def alert(self, text: str):
        """Alarme gehen in den Kanal (falls verbunden), sonst privat an den Besitzer."""
        await self.send(text, chat_id=self.alert_chat or None)

    alert_chat: str = ""

    async def delete(self, chat_id, message_id):
        if self.active and message_id:
            await self.http.request("POST", f"{self.base}/deleteMessage",
                                    json={"chat_id": chat_id, "message_id": message_id}, retries=1)

    async def updates(self) -> list[dict]:
        if not self.active:
            return []
        r = await self.http.request("GET", f"{self.base}/getUpdates",
                                    params={"offset": self.offset, "timeout": 25}, timeout=35, retries=1)
        if r is None or r.status_code != 200:
            return []
        res = r.json().get("result", [])
        if res:
            self.offset = res[-1]["update_id"] + 1
        return res

    async def set_commands(self):
        if not self.active:
            return
        cmds = [
            ("check", "Coin prüfen: /check <adresse>"),
            ("watch", "Wallet beobachten: /watch <adresse> [name]"),
            ("unwatch", "Wallet entfernen: /unwatch <adresse|name>"),
            ("wallets", "Beobachtete Wallets anzeigen"),
            ("smart", "Was kauft Smart Money gerade"),
            ("calls", "Bisherige Calls mit Hoch & x-Faktor"),
            ("bilanz", "Gewinn/Verlust-Rechnung aller Calls"),
            ("ki_test", "KI-Analyse prüfen"),
            ("trends", "Aktuelle News-Trends"),
            ("hot", "Meisterwähnte Coins auf X & Telegram"),
            ("hype", "Hype messen: /hype <adresse>"),
            ("x_feed", "Neueste Promi-Posts"),
            ("x_test", "X/Twitter-Verbindung prüfen"),
            ("tg_status", "Telegram-Mitlesen Status"),
            ("tg_join", "Kanal mitlesen: /tg_join @kanal"),
            ("get", "Einstellungen ansehen"),
            ("set", "Einstellung ändern: /set name wert"),
            ("update", "Neueste Version laden"),
            ("status", "Bot-Status"),
            ("kanal_aus", "Alarme wieder nur privat"),
            ("pause", "Coin-Alarme pausieren"),
            ("resume", "Coin-Alarme fortsetzen"),
            ("hilfe", "Hilfe"),
        ]
        await self.http.request("POST", f"{self.base}/setMyCommands",
                                json={"commands": [{"command": c, "description": d} for c, d in cmds]})


def _chunks(text, n):
    while len(text) > n:
        cut = text.rfind("\n", 0, n)
        cut = cut if cut > 0 else n
        yield text[:cut]
        text = text[cut:]
    yield text
