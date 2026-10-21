"""Claude-Anbindung (Anthropic Messages API, ohne SDK)."""
import json
import logging
import re

log = logging.getLogger("ai")
API = "https://api.anthropic.com/v1/messages"


class AI:
    def __init__(self, http, settings, db):
        self.http = http
        self.s = settings
        self.db = db

    @property
    def cfg(self) -> dict:
        return self.s.section("ai")

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.get("enabled", True) and self.s.anthropic_key)

    BUDGET_KEYS = {"coin": "max_analyses_per_hour", "trend": "max_trend_calls_per_hour", "x": "max_x_calls_per_hour"}

    def _budget_ok(self, kind) -> bool:
        limit = int(self.cfg.get(self.BUDGET_KEYS.get(kind, "max_analyses_per_hour"), 10))
        return self.db.ai_calls_last_hour(kind) < limit

    async def _call(self, model, system, user, max_tokens=900, web_search=False, kind="coin") -> str | None:
        if not self.enabled or not self._budget_ok(kind):
            return None
        body = {
            "model": model, "max_tokens": max_tokens, "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        if web_search:
            body["tools"] = [{"type": "web_search_20250305", "name": "web_search",
                              "max_uses": int(self.cfg.get("web_search_max_uses", 3))}]
        self.db.log_ai_call(kind)
        r = await self._post(body)
        if r is not None and r.status_code == 400 and "tools" in body:
            log.warning("Websuche nicht verfügbar (%s) – versuche ohne", r.text[:200])
            body.pop("tools", None)
            r = await self._post(body)
        if r is None or r.status_code != 200:
            self._status(False, r)
            return None
        self._status(True, r)
        data = r.json()
        return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text").strip()

    # Anmeldung: klassischer API-Key (x-api-key) oder Bearer-Token – das Funktionierende wird gemerkt
    AUTH_MODES = ("x-api-key", "bearer")
    FALLBACK_MODELS = ("claude-sonnet-5-5", "claude-haiku-5-5", "claude-sonnet-4-5", "claude-haiku-4-5")

    def _headers(self, mode):
        h = {"anthropic-version": "2023-06-01", "content-type": "application/json"}
        if mode == "bearer":
            h["Authorization"] = f"Bearer {self.s.anthropic_key}"
        else:
            h["x-api-key"] = self.s.anthropic_key
        return h

    async def _post(self, body):
        modes = [self.db.kv_get("ai_auth")] if self.db.kv_get("ai_auth") else list(self.AUTH_MODES)
        r = None
        for mode in modes:
            r = await self.http.request("POST", API, json=body, headers=self._headers(mode), retries=2, timeout=120)
            if r is not None and r.status_code in (401, 403) and len(modes) > 1:
                continue
            if r is not None and r.status_code == 200:
                self.db.kv_set("ai_auth", mode)
            break
        # Modell unbekannt? -> nächstes Modell probieren
        if r is not None and r.status_code == 404 or (r is not None and r.status_code == 400 and "model" in r.text.lower()):
            for mdl in self.FALLBACK_MODELS:
                if mdl == body["model"]:
                    continue
                r2 = await self.http.request("POST", API, json={**body, "model": mdl},
                                             headers=self._headers(self.db.kv_get("ai_auth") or modes[0]), retries=1, timeout=120)
                if r2 is not None and r2.status_code == 200:
                    log.warning("Modell %s nicht verfügbar – nutze %s", body["model"], mdl)
                    self.s.overrides["ai.analysis_model"] = self.s.overrides["ai.trend_model"] = mdl
                    return r2
        return r

    def _status(self, ok, r):
        code = r.status_code if r is not None else 0
        msg = "" if ok else (r.text[:300] if r is not None else "keine Antwort (Netzwerk)")
        if not ok:
            log.error("Claude-Fehler %s: %s", code, msg)
        self.db.kv_set("ai_status", {"ok": ok, "code": code, "msg": msg, "ts": __import__("time").time()})

    async def selftest(self) -> str:
        """Für /ki_test: eine Mini-Anfrage und eine verständliche Diagnose."""
        if not self.s.anthropic_key:
            return "❌ Kein Claude-Schlüssel eingetragen (ANTHROPIC_API_KEY fehlt)."
        body = {"model": self.cfg.get("trend_model", "claude-haiku-5-5"), "max_tokens": 20,
                "messages": [{"role": "user", "content": "Antworte nur mit: OK"}]}
        self.db.kv_set("ai_auth", None)
        r = await self._post(body)
        self._status(r is not None and r.status_code == 200, r)
        if r is not None and r.status_code == 200:
            return f"✅ KI funktioniert (Anmeldung: {self.db.kv_get('ai_auth')}, Modell: {self.cfg.get('trend_model')})."
        code = r.status_code if r is not None else 0
        hint = {401: "Schlüssel ungültig – neuen API-Key erstellen (beginnt mit sk-ant-api…).",
                403: "Schlüssel hat keine Berechtigung (falscher Workspace?).",
                400: "Anfrage abgelehnt – evtl. Guthaben leer oder Modellname falsch.",
                429: "Zu viele Anfragen / Limit erreicht.",
                0: "Server erreicht api.anthropic.com nicht."}.get(code, "Unbekannter Fehler.")
        return f"❌ KI-Fehler {code}: {hint}\n<code>{__import__('html').escape(r.text[:250]) if r is not None else ''}</code>"

    # ---------------- Trend-Stichwörter aus Schlagzeilen ----------------
    async def extract_trends(self, headlines: list[str]) -> list[dict] | None:
        system = ("Du bist ein Trend-Scout für Meme Coins mit Fokus auf die USA und Krypto-Twitter. Du findest in "
                  "Schlagzeilen und Posts die Themen, zu denen JETZT Meme Coins entstehen oder pumpen: Trump und Familie, "
                  "Elon Musk, US-Politik, Promis, virale Tiere/Memes, Tech-Produkte, Sport-Events, Krypto-KOLs. "
                  "Bevorzuge frische, emotionale, leicht 'memebare' Themen. Antworte NUR mit JSON.")
        user = ("Schlagzeilen:\n" + "\n".join(f"- {h}" for h in headlines[:150]) +
                "\n\nGib die 12 vielversprechendsten Themen als JSON-Liste zurück: "
                '[{"keyword": "kurzes Suchwort (1-2 Wörter, englisch wie ein Coin-Name, z.B. \\"Cybercab\\")", '
                '"headline": "passende Schlagzeile", "why": "warum Meme-Potenzial (max 12 Wörter, deutsch)"}]')
        txt = await self._call(self.cfg.get("trend_model", "claude-haiku-5-5"), system, user, max_tokens=1200,
                               kind="trend")
        if not txt:
            return None
        m = re.search(r"\[.*\]", txt, re.S)
        try:
            items = json.loads(m.group(0)) if m else []
            return [i for i in items if isinstance(i, dict) and i.get("keyword")]
        except (ValueError, AttributeError):
            return None

    # ---------------- Stichwörter aus X-Posts von Promis/KOLs ----------------
    async def keywords_from_posts(self, posts: list[dict]) -> list[dict]:
        if not posts:
            return []
        system = ("Du liest frische X-Posts von Promis und Krypto-Influencern und erkennst, woraus in den nächsten "
                  "Minuten ein Meme Coin entstehen könnte (Wörter, Namen, Tiere, Sprüche, Ticker). Antworte NUR mit JSON.")
        lines = "\n".join(f"- @{p['user']}: {p['text'][:280]}" for p in posts[:40])
        user = (f"Posts:\n{lines}\n\nJSON-Liste mit max. 8 Einträgen: "
                '[{"keyword": "1-2 Wörter wie ein Coin-Name", "user": "@name", "why": "max 10 Wörter, deutsch"}]. '
                "Leere Liste [] wenn nichts memebar ist.")
        txt = await self._call(self.cfg.get("trend_model", "claude-haiku-5-5"), system, user, max_tokens=700, kind="x")
        m = re.search(r"\[.*\]", txt or "", re.S)
        try:
            return [i for i in json.loads(m.group(0)) if isinstance(i, dict) and i.get("keyword")] if m else []
        except ValueError:
            return []

    # ---------------- Coin-Einschätzung ----------------
    async def analyze_coin(self, ctx: dict) -> tuple[str | None, int | None]:
        system = (
            "Du bist ein nüchterner, skeptischer Krypto-Analyst für Meme Coins. Du schreibst auf Deutsch, kurz und "
            "klar für einen Privatanleger. Du findest heraus, WAS hinter dem Coin steckt (Narrativ, Bezug zu echten "
            "News/Personen/Produkten), WOHER der Hype kommt (X/Twitter, Telegram, KOLs, News, bezahlte Boosts) und "
            "ob der Hype echt/organisch oder künstlich wirkt. Erfinde nichts – wenn du etwas nicht findest, sag es. "
            "Gib niemals eine Garantie; die meisten Meme Coins verlieren fast ihren gesamten Wert."
        )
        user = (
            "Analysiere diesen Coin. Nutze die Websuche (falls verfügbar), um Erwähnungen auf X/Twitter, "
            "News und die Projektseite zu prüfen. Suche gezielt nach Warnsignalen: '<Name> scam', '<Name> rug', "
            "Fake-/Kopie-Token einer echten Marke oder Person, Entwickler mit früheren Rugs, bezahlte Influencer-Pushes, "
            "Insider-/Bundle-Käufe, gelöschte oder neue Social-Accounts.\n\n"
            f"DATEN:\n{json.dumps(ctx, ensure_ascii=False, indent=1, default=str)}\n\n"
            "Antworte GENAU in diesem Format (max. 140 Wörter, ohne Markdown-Sternchen):\n"
            "🧠 Dahinter: <worum geht es, Bezug zu echtem Ereignis?>\n"
            "📣 Hype-Quelle: <woher kommt die Aufmerksamkeit, organisch oder gekauft?>\n"
            "👍 Pro: <1-2 Punkte>\n"
            "👎 Contra: <1-2 Punkte>\n"
            "🚨 Gefahren: <konkrete Warnsignale aus der Recherche oder 'keine gefunden'>\n"
            "🎯 Fazit: <ein Satz>\n"
            "HYPE_SCORE: <0-100, wie stark & echt ist der Hype/das Narrativ>"
        )
        txt = await self._call(self.cfg.get("analysis_model", "claude-sonnet-5-5"), system, user,
                               max_tokens=1500, web_search=bool(self.cfg.get("web_search", True)))
        if not txt:
            return None, None
        m = re.search(r"HYPE_SCORE:\s*(\d{1,3})", txt)
        score = min(100, int(m.group(1))) if m else None
        # Nur den Teil ab der ersten Format-Zeile behalten
        start = txt.find("🧠")
        body = txt[start:] if start >= 0 else txt
        body = re.sub(r"\n?HYPE_SCORE:.*", "", body).strip()
        return body, score
