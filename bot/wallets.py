"""Wallet-Tracking: Was kaufen/verkaufen die Wallets, die du beobachtest?"""
import logging
from html import escape

from .chains import CHAINS, EVM_CHAINS, norm
from .fmt import age, links, price, usd
from .market import IGNORE_SYMBOLS, pair_metrics

log = logging.getLogger("wallets")
SPL = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
SPL22 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCxEpPxuEb"


class WalletTracker:
    def __init__(self, http, settings, db, market, security, tg, scanner):
        self.http, self.s, self.db = http, settings, db
        self.market, self.sec, self.tg, self.scanner = market, security, tg, scanner
        self.cfg = settings.get("wallets", default={}) or {}

    async def run_cycle(self):
        for w in self.db.wallets():
            try:
                if w["chain"] == "solana":
                    await self._solana(w["address"], w["label"])
                else:
                    for chain in EVM_CHAINS:
                        if chain in self.s.chains:
                            await self._evm(chain, w["address"], w["label"])
            except Exception:
                log.exception("Wallet-Fehler %s", w["address"])

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
            return
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
    async def _sol_holdings(self, wallet) -> dict[str, float] | None:
        out: dict[str, float] = {}
        for prog in (SPL, SPL22):
            res = await self.http.post_json(self.s.solana_rpc, {
                "jsonrpc": "2.0", "id": 1, "method": "getTokenAccountsByOwner",
                "params": [wallet, {"programId": prog}, {"encoding": "jsonParsed", "commitment": "confirmed"}],
            })
            if not res or "result" not in res:
                return None
            for acc in res["result"].get("value", []):
                info = acc["account"]["data"]["parsed"]["info"]
                amt = float(info["tokenAmount"].get("uiAmount") or 0)
                out[info["mint"]] = out.get(info["mint"], 0) + amt
        return out

    async def _solana(self, wallet, label):
        now = await self._sol_holdings(wallet)
        if now is None:
            return
        key = f"sol_snapshot:{wallet}"
        before = self.db.kv_get(key)
        self.db.kv_set(key, now)
        if before is None:
            return  # erster Durchlauf: nur merken
        for mint in set(now) | set(before):
            a, b = before.get(mint, 0.0), now.get(mint, 0.0)
            if b > a * 1.05 and b - a > 0:
                await self._report("solana", wallet, label, mint, "in", b - a, new_position=(a == 0))
            elif b < a * 0.95:
                await self._report("solana", wallet, label, mint, "out", a - b, closed=(b == 0))

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

        if direction == "in":
            sec = await self.sec.check(chain, token, m["pair"])
            if not sec.passed and not self.cfg.get("alert_on_unsafe", True):
                return
            verdict = "✅ Sicherheits-Checks bestanden" if sec.passed else \
                "🚨 ACHTUNG: " + escape("; ".join(sec.hard_fails[:4]))
            title = "🐋 WALLET KAUFT" + (" (neue Position)" if new_position else " (nachgekauft)")
            # in die normale Coin-Pipeline aufnehmen
            self.db.add_candidate(chain, token, f"Wallet {label}", m["symbol"])
        else:
            verdict = ""
            title = "📤 WALLET VERKAUFT" + (" (komplett raus)" if closed else "")

        lines = [
            f"<b>{title}</b>",
            f"👛 <b>{escape(label)}</b> <code>{wallet[:6]}…{wallet[-4:]}</code> · {CHAINS[chain]['name']}",
            "",
            f"<b>{escape(m['name'])}</b> (${escape(m['symbol'])})",
            f"<code>{token}</code>",
            f"Menge: {amount:,.0f} ≈ <b>{usd(value)}</b>",
            f"💰 Preis {price(m['price'])} · MCap {usd(m['mcap'])} · Liq {usd(m['liquidity'])} · Alter {age(m['age_min'])}",
        ]
        if verdict:
            lines += ["", verdict]
        lines += ["", links(chain, token, m)]
        await self.tg.alert("\n".join(lines))
