"""Wallet-Tracking: Was kaufen/verkaufen die Wallets, die du beobachtest?"""
import logging
import time
from html import escape

from .chains import CHAINS, EVM_CHAINS, norm
from .fmt import age, links, price, usd
from .market import IGNORE_SYMBOLS, pair_metrics

log = logging.getLogger("wallets")
SPL = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
SPL22 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCxEpPxuEb"
SOL_IGNORE_MINTS = {"So11111111111111111111111111111111111111112",   # wSOL
                    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",   # USDC
                    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"}   # USDT


def token_deltas(meta, owner) -> dict[str, tuple[float, float]]:
    """Token-Bestand der Wallet vor/nach einer Transaktion, je Mint."""
    out: dict[str, list[float]] = {}
    for idx, field in ((0, "preTokenBalances"), (1, "postTokenBalances")):
        for b in meta.get(field) or []:
            if b.get("owner") != owner:
                continue
            amt = float((b.get("uiTokenAmount") or {}).get("uiAmount") or 0)
            out.setdefault(b["mint"], [0.0, 0.0])[idx] += amt
    return {k: (v[0], v[1]) for k, v in out.items()}


class WalletTracker:
    def __init__(self, http, settings, db, market, security, tg, scanner):
        self.http, self.s, self.db = http, settings, db
        self.market, self.sec, self.tg, self.scanner = market, security, tg, scanner
        self.stats = {"sol_ok": 0, "sol_fail": 0, "sol_tx": 0, "sol_last": 0, "sol_secs": 0,
                      "evm_ok": 0, "evm_fail": 0, "evm_last": 0, "trades": 0}

    @property
    def cfg(self):
        return self.s.section("wallets")

    def import_list(self) -> int:
        """Wallet-Liste aus config.yaml übernehmen. Neue list_version = alte Listen-Wallets ersetzen
        (mit /watch selbst hinzugefügte bleiben, mit /unwatch entfernte kommen nicht zurück)."""
        version = self.cfg.get("list_version", 1)
        done = set(self.db.kv_get("wallet_list_done", []))
        if self.db.kv_get("wallet_list_version", 1) != version:
            for a in done:
                self.db.con.execute("DELETE FROM wallets WHERE address=? OR address=?", (a, a.lower()))
            self.db.con.commit()
            done = set()
            self.db.kv_set("wallet_list_version", version)
        n = 0
        for entry in self.cfg.get("list") or []:
            label, *addrs = entry
            for a in addrs:
                if not a or a in done:
                    continue
                if a.startswith("0x"):
                    self.db.add_wallet("evm", a.lower(), label)
                else:
                    self.db.add_wallet("solana", a, label)
                done.add(a)
                n += 1
        self.db.kv_set("wallet_list_done", sorted(done))
        return n

    async def run_cycle(self):
        await self.run_solana()
        await self.run_evm()

    async def run_solana(self):
        t0 = time.time()
        for k in ("sol_ok", "sol_fail", "sol_tx"):
            self.stats[k] = 0
        for w in self.db.wallets():
            if w["chain"] == "solana":
                try:
                    await self._solana(w["address"], w["label"])
                except Exception:
                    log.exception("Wallet-Fehler %s", w["address"])
        self.stats["sol_last"] = time.time()
        self.stats["sol_secs"] = time.time() - t0
        self.db.kv_set("wallet_stats", self.stats)

    async def run_evm(self):
        self.stats["evm_ok"] = self.stats["evm_fail"] = 0
        for w in self.db.wallets():
            if w["chain"] == "solana":
                continue
            for chain in EVM_CHAINS:
                if chain in self.s.chains and (self.s.etherscan_key or CHAINS[chain]["blockscout"]):
                    try:
                        await self._evm(chain, w["address"], w["label"])
                    except Exception:
                        log.exception("Wallet-Fehler %s %s", chain, w["address"])
        self.stats["evm_last"] = time.time()
        self.db.kv_set("wallet_stats", self.stats)

    # ------------------------- EVM -------------------------
    async def _fetch_tokentx(self, chain, wallet) -> list[dict] | None:
        cid = CHAINS[chain]["evm_chainid"]
        q = f"module=account&action=tokentx&address={wallet}&page=1&offset=50&sort=desc"
        if self.s.etherscan_key:
            data = await self.http.get_json(
                f"https://api.etherscan.io/v2/api?chainid={cid}&{q}&apikey={self.s.etherscan_key}")
            if data and str(data.get("status")) == "1" and isinstance(data.get("result"), list):
                return data["result"]
            if data and "No transactions" in str(data.get("message", "")):
                return []
        bs = CHAINS[chain]["blockscout"]
        if bs:
            data = await self.http.get_json(f"{bs}/api?{q}")
            if data and isinstance(data.get("result"), list):
                return data["result"]
        return None

    async def _evm(self, chain, wallet, label):
        txs = await self._fetch_tokentx(chain, wallet)
        if txs is None:
            self.stats["evm_fail"] += 1
            return
        self.stats["evm_ok"] += 1
        init_key = f"wallet_init:{chain}:{wallet}"
        first_run = not self.db.kv_get(init_key)
        w = wallet.lower()
        # pro Transaktion + Token zusammenfassen
        events: dict[tuple, dict] = {}
        for t in reversed(txs):
            key = f"{t.get('hash')}:{t.get('contractAddress')}:{t.get('from')}:{t.get('to')}"
            if self.db.wallet_seen(chain, wallet, key):
                continue
            self.db.mark_wallet_seen(chain, wallet, key)
            if first_run:
                continue
            sym = (t.get("tokenSymbol") or "").upper()
            if sym in IGNORE_SYMBOLS:
                continue
            direction = "in" if (t.get("to") or "").lower() == w else ("out" if (t.get("from") or "").lower() == w else None)
            if not direction:
                continue
            try:
                amount = int(t.get("value", 0)) / 10 ** int(t.get("tokenDecimal") or 18)
            except (ValueError, TypeError):
                amount = 0
            ek = (t.get("hash"), (t.get("contractAddress") or "").lower(), direction)
            e = events.setdefault(ek, {"token": ek[1], "dir": direction, "amount": 0.0,
                                       "symbol": t.get("tokenSymbol"), "name": t.get("tokenName")})
            e["amount"] += amount
        if first_run:
            self.db.kv_set(init_key, True)
            return
        for (_, token, _), e in events.items():
            await self._report(chain, wallet, label, token, e["dir"], e["amount"])

    # ------------------------- Solana -------------------------
    async def _rpc(self, method, params):
        res = await self.http.post_json(self.s.solana_rpc, {"jsonrpc": "2.0", "id": 1, "method": method,
                                                             "params": params})
        if not res or "result" not in res:
            return None
        return res["result"]

    async def _solana(self, wallet, label):
        """Neue Transaktionen der Wallet lesen und Token-Saldo-Änderungen auswerten (Kauf/Verkauf)."""
        sigs = await self._rpc("getSignaturesForAddress", [wallet, {"limit": 15, "commitment": "confirmed"}])
        if sigs is None:
            self.stats["sol_fail"] += 1
            return
        self.stats["sol_ok"] += 1
        key = f"solsig:{wallet}"
        last = self.db.kv_get(key)
        if sigs:
            self.db.kv_set(key, sigs[0]["signature"])
        if last is None:
            return  # erster Durchlauf: nur merken
        new = []
        for sg in sigs:
            if sg["signature"] == last:
                break
            if not sg.get("err"):
                new.append(sg["signature"])
        for sig in reversed(new[:6]):
            tx = await self._rpc("getTransaction", [sig, {"encoding": "jsonParsed", "commitment": "confirmed",
                                                          "maxSupportedTransactionVersion": 0}])
            if not tx or not tx.get("meta"):
                continue
            self.stats["sol_tx"] += 1
            for mint, (pre, post) in token_deltas(tx["meta"], wallet).items():
                if mint in SOL_IGNORE_MINTS:
                    continue
                if post > pre * 1.02 and post - pre > 0:
                    await self._report("solana", wallet, label, mint, "in", post - pre, new_position=(pre == 0))
                elif post < pre * 0.98:
                    await self._report("solana", wallet, label, mint, "out", pre - post, closed=(post == 0))

    # ------------------------- Meldung -------------------------
    async def _report(self, chain, wallet, label, token, direction, amount, new_position=False, closed=False):
        token = norm(chain, token)
        pair = await self.market.pair_for_token(chain, token)
        if not pair:
            return  # kein Markt -> meist Spam-Airdrop
        m = pair_metrics(pair)
        if m["symbol"].upper() in IGNORE_SYMBOLS or m["liquidity"] < 1000:
            return
        value = amount * m["price"]
        if value < self.cfg.get("min_buy_usd", 100):
            return
        self.db.add_wallet_trade(chain, token, wallet, label, direction, value, m["mcap"], closed)
        self.stats["trades"] += 1

        hours = self.cfg.get("cluster_hours", 12)
        need = self.cfg.get("cluster_min_wallets", 4)
        buyers = self.db.wallet_buyers(token, hours)
        holding = [b for b in buyers if not b["exited"]]

        if direction == "in":
            self.db.add_candidate(chain, token, f"Wallet {label}", m["symbol"])
            done = self.db.kv_get(f"cluster:{token}", 0)
            n = len(holding)
            if n >= need and done == 0:
                self.db.kv_set(f"cluster:{token}", n)
                return await self.cluster_call(chain, token, pair, buyers, first=True)
            nxt = next((x for x in (6, 10, 15, 20, 30) if x > done), 999)
            if done and n >= nxt:
                self.db.kv_set(f"cluster:{token}", n)
                names = ", ".join(escape(b["label"]) for b in holding[-(n - done):])
                return await self.tg.alert(
                    f"<b>🐋 ${escape(m['symbol'])}: jetzt {n} Smart-Money-Wallets drin</b>\n"
                    f"Neu dazu: {names}\nMCap {usd(m['mcap'])} · 1h {m['chg_1h']:+.0f}%\n{links(chain, token, m)}")
            await self._single(chain, wallet, label, token, m, value, new_position, n, need)
        else:
            if self.db.kv_get(f"cluster:{token}", 0) and not self.db.kv_get(f"cluster_exit:{token}"):
                out = [b for b in buyers if b["exited"]]
                if len(out) >= max(2, len(buyers) / 2):
                    self.db.kv_set(f"cluster_exit:{token}", True)
                    names = ", ".join(escape(b["label"]) for b in out[:8])
                    await self.tg.alert(
                        f"<b>⚠️ SMART MONEY STEIGT AUS – ${escape(m['symbol'])}</b>\n"
                        f"{len(out)} von {len(buyers)} Wallets sind komplett raus: {names}\n"
                        f"MCap jetzt {usd(m['mcap'])} · {links(chain, token, m)}\n"
                        "<i>Wenn du drin bist: Gewinne sichern oder Stop-Loss enger ziehen.</i>")
                    return
            if self.cfg.get("sell_alerts"):
                await self.tg.send(
                    f"📤 <b>{escape(label)}</b> verkauft ${escape(m['symbol'])} ≈ {usd(value)}"
                    + (" (komplett raus)" if closed else "") + f" · MCap {usd(m['mcap'])}")

    async def _single(self, chain, wallet, label, token, m, value, new_position, n, need):
        mode = self.cfg.get("single_alerts", "private")
        if mode == "off":
            return
        last = self.db.kv_get(f"wsingle:{token}", 0)
        if time.time() - last < 1200:   # pro Coin max. alle 20 Min eine Einzelmeldung
            return
        self.db.kv_set(f"wsingle:{token}", time.time())
        sec = await self.sec.check(chain, token, m["pair"])
        if not sec.passed and not self.cfg.get("alert_on_unsafe", True):
            return
        verdict, _ = quick_verdict(sec, m, self.s)
        lines = [
            f"<b>🐋 {escape(label)} kauft</b>" + (" (neue Position)" if new_position else ""),
            f"<b>{escape(m['name'])}</b> (${escape(m['symbol'])}) · {CHAINS[chain]['name']} · ≈ <b>{usd(value)}</b>",
            f"<code>{token}</code>",
            f"💰 MCap {usd(m['mcap'])} · Liq {usd(m['liquidity'])} · Alter {age(m['age_min'])} · 1h {m['chg_1h']:+.0f}%",
            f"👥 Smart Money drin: <b>{n}</b>/{need} (ab {need} kommt ein Call mit KI-Meinung)",
            "", verdict, "", links(chain, token, m),
        ]
        text = "\n".join(lines)
        await (self.tg.alert(text) if mode == "channel" else self.tg.send(text))

    async def cluster_call(self, chain, token, pair, buyers, first=True):
        """Mehrere Smart-Money-Wallets im selben Coin -> Call (immer), mit ehrlicher Einschätzung."""
        from .fmt import coin_message
        holding = [b for b in buyers if not b["exited"]]
        n = len(holding)
        ev = await self.scanner.evaluate(chain, token, f"Smart Money: {n} Wallets", pair=pair, force_ai=True,
                                         extra={"smart_money_kaeufer": [
                                             {"name": b["label"], "usd": round(b["usd"]), "einstieg_mcap": b["entry_mcap"],
                                              "vor_min": round((time.time() - b["ts"]) / 60)} for b in buyers]})
        m = ev.metrics
        verdict, ok = quick_verdict(ev.security, m, self.s, ev=ev, buyers=holding)
        entry = min((b["entry_mcap"] for b in holding if b["entry_mcap"]), default=None)
        blines = [f"• <b>{escape(b['label'])}</b> {usd(b['usd'])} bei MC {usd(b['entry_mcap'])} "
                  f"({(time.time() - b['ts']) / 60:.0f} min her){' – schon raus' if b['exited'] else ''}"
                  for b in buyers[:12]]
        head = (f"🐋 SMART-MONEY-CALL – {n} Wallets kaufen" if first else f"🐋 SMART MONEY: jetzt {n} Wallets drin")
        body = coin_message(ev, header=head)
        extra = ["", "<b>👥 Wer ist drin:</b>"] + blines
        if entry:
            extra.append(f"Erster Einstieg bei MC {usd(entry)} → jetzt {usd(m['mcap'])} ({m['mcap'] / entry:.1f}x)")
        extra += ["", f"<b>🧭 Meine Meinung:</b> {verdict}"]
        # Meinung + Wallets direkt unter die Kopfzeile setzen
        top, rest = body.split("\n", 1)
        await self.tg.alert(top + "\n" + "\n".join(extra) + "\n" + rest)
        if first and not (ev.security and ev.security.fatal):
            self.db.add_call(chain, token, m, ev.total, f"Smart Money ({n})", f"{n} Wallets")
            self.db.mark(chain, token, "alerted", f"Smart Money {n} Wallets")


def quick_verdict(sec, m, s, ev=None, buyers=None) -> tuple[str, bool]:
    """Kurze, ehrliche Einschätzung: lohnt sich ein Einstieg?"""
    if sec is None:
        return "❔ Konnte die Sicherheit nicht prüfen – Finger weg, bis es klappt.", False
    if sec.fatal:
        return "⛔ <b>FINGER WEG</b> – " + escape("; ".join(sec.hard_fails[:3])) + \
            ". Auch Profis kaufen manchmal Fallen oder spielen bewusst nur Sekunden-Trades.", False
    if not sec.passed:
        return "⚠️ <b>Riskant</b> – " + escape("; ".join(sec.hard_fails[:3])) + \
            ". Wenn überhaupt, nur Mini-Einsatz.", False
    probs = []
    entry = min((b["entry_mcap"] for b in (buyers or []) if b.get("entry_mcap")), default=None)
    if entry and m["mcap"] > entry * 2.5:
        probs.append(f"du wärst spät dran (schon {m['mcap'] / entry:.1f}x seit dem ersten Wallet-Kauf)")
    if m["chg_5m"] > 25:
        probs.append(f"gerade +{m['chg_5m']:.0f} % in 5 min – auf Rücksetzer warten")
    if m["liquidity"] < 15000:
        probs.append("wenig Liquidität – schwer wieder rauszukommen")
    if m["mcap"] > 3_000_000:
        probs.append("Market Cap schon hoch – weniger Luft nach oben")
    score = ev.total if ev else None
    if not probs and (score is None or score >= s.get("min_score", default=65) - 5):
        return ("✅ <b>Einstieg vertretbar</b> – Sicherheit ok, Smart Money ist drin. "
                "Klein einsteigen, bei 2x den Einsatz rausnehmen, Stop-Loss bei −50 %."), True
    if not probs:
        probs.append(f"Gesamt-Score nur {score}/100")
    return "🟡 <b>Nur mit kleinem Einsatz / abwarten</b> – " + escape("; ".join(probs)) + ".", False
