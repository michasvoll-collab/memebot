"""Offline-Test mit nachgebauten API-Antworten (kein Internet nötig).

    python tests/test_offline.py
"""
import asyncio
import os
import sys
import tempfile
import time
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
if "httpx" not in sys.modules:  # Test läuft auch ohne installiertes httpx
    try:
        import httpx  # noqa
    except ImportError:
        sys.modules["httpx"] = types.SimpleNamespace(AsyncClient=lambda *a, **k: None, Response=object, Timeout=lambda *a, **k: None,
                                                     HTTPError=Exception)

os.environ["DRY_RUN"] = "1"
os.environ["ANTHROPIC_API_KEY"] = "test"
os.environ["ETHERSCAN_API_KEY"] = "test"
os.environ["TELEGRAM_CHAT_ID"] = "1"
os.environ["MEMEBOT_DB"] = tempfile.mktemp(suffix=".db")

NOW_MS = time.time() * 1000
SOL_GOOD = "GoodMemeSoL1111111111111111111111111111111"
SOL_RUG = "RugMemeSoL11111111111111111111111111111111"
EVM_GOOD = "0x" + "a" * 40
EVM_HONEY = "0x" + "b" * 40
WALLET_EVM = "0x" + "c" * 40
WALLET_SOL = "WaLLetSoL1111111111111111111111111111111111"


def pair(chain, addr, sym, liq=60000, mcap=150000, age_h=3, buys=400, sells=250, chg1h=35, socials=True):
    return {
        "chainId": chain, "dexId": "raydium" if chain == "solana" else "uniswap",
        "url": f"https://dexscreener.com/{chain}/{addr}", "pairAddress": "PAIR" + addr[-6:],
        "baseToken": {"address": addr, "name": f"{sym} Coin", "symbol": sym},
        "quoteToken": {"address": "So11111111111111111111111111111111111111112", "symbol": "SOL"},
        "priceUsd": "0.000412", "liquidity": {"usd": liq}, "marketCap": mcap, "fdv": mcap,
        "volume": {"m5": 4000, "h1": 45000, "h6": 120000, "h24": 300000},
        "txns": {"h1": {"buys": buys, "sells": sells}},
        "priceChange": {"m5": 3, "h1": chg1h, "h6": 80, "h24": 150},
        "pairCreatedAt": NOW_MS - age_h * 3.6e6,
        "info": {"socials": [{"type": "twitter", "url": "https://x.com/cybercab"}],
                 "websites": [{"url": "https://cybercab.meme"}]} if socials else None,
    }


PAIRS = {
    SOL_GOOD: pair("solana", SOL_GOOD, "CYBERCAB"),
    SOL_RUG: pair("solana", SOL_RUG, "RUGGY"),
    EVM_GOOD: pair("base", EVM_GOOD, "TESLADOG"),
    EVM_HONEY: pair("base", EVM_HONEY, "HONEY"),
}

RSS = """<rss><channel>
<item><title>Tesla unveils Cybercab robotaxi production model - Reuters</title><link>x</link></item>
<item><title>Musk says Cybercab will launch in 2027 - CNBC</title><link>y</link></item>
<item><title>Bitcoin hits new high as ETF flows surge - CoinDesk</title><link>z</link></item>
</channel></rss>"""

calls = []


class Resp:
    def __init__(self, data, status=200):
        self._d, self.status_code, self.text = data, status, str(data)

    def json(self):
        return self._d


class FakeHttp:
    async def get_json(self, url, **kw):
        calls.append(url)
        if "token-profiles/latest" in url:
            return [{"chainId": "solana", "tokenAddress": SOL_GOOD}, {"chainId": "solana", "tokenAddress": SOL_RUG}]
        if "token-boosts" in url or "community-takeovers" in url:
            return [{"chainId": "base", "tokenAddress": EVM_GOOD}]
        if "geckoterminal" in url:
            if "/base/" in url:
                return {"data": [{"relationships": {"base_token": {"data": {"id": f"base_{EVM_HONEY}"}}}}]}
            return {"data": []}
        if "/tokens/v1/" in url:
            addrs = url.rsplit("/", 1)[1].split(",")
            return [PAIRS[a] for a in PAIRS if a.lower() in [x.lower() for x in addrs]]
        if "/latest/dex/tokens/" in url:
            a = url.rsplit("/", 1)[1]
            return {"pairs": [PAIRS[k] for k in PAIRS if k.lower() == a.lower()]}
        if "/latest/dex/search" in url:
            return {"pairs": [PAIRS[SOL_GOOD], PAIRS[EVM_GOOD]]}
        if "honeypot.is" in url:
            if EVM_HONEY in url:
                return {"honeypotResult": {"isHoneypot": True, "honeypotReason": "Sell reverted"},
                        "simulationSuccess": True, "simulationResult": {"buyTax": 0, "sellTax": 100}}
            return {"honeypotResult": {"isHoneypot": False}, "simulationSuccess": True,
                    "simulationResult": {"buyTax": 0, "sellTax": 0}, "summary": {"risk": "low"}}
        if "gopluslabs.io/api/v1/token_security" in url:
            a = url.split("contract_addresses=")[1].lower()
            return {"code": 1, "result": {a: {
                "is_honeypot": "1" if a == EVM_HONEY else "0", "is_open_source": "1", "is_mintable": "0",
                "owner_address": "0x0000000000000000000000000000000000000000",
                "buy_tax": "0", "sell_tax": "0", "creator_percent": "0.01", "holder_count": "1834",
                "lp_holders": [{"address": "0x000000000000000000000000000000000000dead", "percent": "0.97", "is_locked": 0}],
                "holders": [{"address": "PAIR" + a[-6:], "percent": "0.20"},
                            {"address": "0x1", "percent": "0.04"}, {"address": "0x2", "percent": "0.03"}],
            }}}
        if "gopluslabs.io/api/v1/solana" in url:
            return {"code": 1, "result": {}}
        if "rugcheck" in url:
            good = SOL_GOOD in url
            return {
                "rugged": False, "mintAuthority": None, "freezeAuthority": None if good else "Abc",
                "token": {"supply": 1e15}, "score": 101 if good else 5000,
                "risks": [] if good else [{"name": "Freeze Authority still enabled", "level": "danger"}],
                "markets": [{"pubkey": "M1", "marketType": "raydium", "lp": {"lpLockedPct": 100 if good else 5, "quoteUSD": 30000}}],
                "topHolders": [{"owner": "M1", "pct": 18}, {"owner": "h1", "pct": 3.1}, {"owner": "h2", "pct": 2.4}],
                "knownAccounts": {"M1": {"type": "AMM"}}, "totalHolders": 2400, "creatorBalance": 0,
            }
        if "etherscan.io/v2" in url:
            if "chainid=8453" not in url:
                return {"status": "0", "message": "No transactions found", "result": []}
            return {"status": "1", "result": calls.count("evm_wallet_round") and [
                {"hash": "0xh2", "from": "0xpool", "to": WALLET_EVM, "contractAddress": EVM_GOOD,
                 "tokenSymbol": "TESLADOG", "tokenName": "Tesla Dog", "value": str(5_000_000 * 10 ** 18), "tokenDecimal": "18"},
                {"hash": "0xh1", "from": "0xpool", "to": WALLET_EVM, "contractAddress": EVM_GOOD,
                 "tokenSymbol": "TESLADOG", "tokenName": "Tesla Dog", "value": "1", "tokenDecimal": "18"}] or [
                {"hash": "0xh1", "from": "0xpool", "to": WALLET_EVM, "contractAddress": EVM_GOOD,
                 "tokenSymbol": "TESLADOG", "tokenName": "Tesla Dog", "value": "1", "tokenDecimal": "18"}]}
        return None

    async def post_json(self, url, payload, **kw):
        prog = payload["params"][1]["programId"]
        if prog.startswith("Tokenz"):
            return {"result": {"value": []}}
        amt = 2_000_000 if calls.count("sol_wallet_round") else 0
        return {"result": {"value": [{"account": {"data": {"parsed": {"info": {
            "mint": SOL_GOOD, "tokenAmount": {"uiAmount": amt}}}}}}] if amt else []}}

    async def get_text(self, url, **kw):
        return RSS

    async def request(self, method, url, **kw):
        if "twitterapi.io" in url:
            q = kw["params"]["query"]
            calls.append("x:" + q)
            if "from:" in q:
                tw = [{"id": "k1", "text": "Robotaxi day! Cybercab is coming", "author": {"userName": "elonmusk", "followers": 2e8}}]
            else:
                tw = [{"id": f"t{i}", "text": "$CYBERCAB sending", "author": {"userName": f"user{i % 7}", "followers": 1000 * i}}
                      for i in range(12)] + [{"id": "big", "text": "CYBERCAB", "author": {"userName": "kol", "followers": 250000}}]
            return Resp({"tweets": tw, "has_next_page": False})
        if "anthropic" in url:
            if "Posts:" in kw["json"]["messages"][0]["content"]:
                return Resp({"content": [{"type": "text", "text":
                             '[{"keyword":"Cybercab","user":"@elonmusk","why":"Elon postet Robotaxi"}]'}]})
            if "Schlagzeilen" in kw["json"]["messages"][0]["content"]:
                return Resp({"content": [{"type": "text", "text":
                             '[{"keyword":"Cybercab","headline":"Tesla unveils Cybercab","why":"Tesla-Hype"}]'}]})
            return Resp({"content": [{"type": "server_tool_use"}, {"type": "text", "text":
                         "Ich habe gesucht.\n🧠 Dahinter: Bezug auf Teslas Cybercab.\n📣 Hype-Quelle: X-Posts.\n"
                         "👍 Pro: aktuelles Thema\n👎 Contra: kein Team\n🎯 Fazit: spekulativ.\nHYPE_SCORE: 72"}]})
        return Resp({}, 200)

    async def close(self):
        pass


async def main():
    import main as m
    app = m.App()
    fake = FakeHttp()
    for obj in (app, app.tg, app.market, app.ai, app.security, app.news, app.wallets, app.xhype):
        obj.http = fake
    app.db.kv_set("x_key", "test")

    # 1) News -> Trends
    await app.news_job()
    trends = app.db.kv_get("trends")
    assert trends and trends[0]["keyword"] == "Cybercab", trends

    # 2) Scan-Durchlauf
    await app.scanner.run_cycle()
    st = {(r["address"]): (r["status"], r["reason"]) for r in app.db.con.execute("SELECT * FROM tokens")}
    print("\nErgebnisse Runde 1:", st)
    assert "Bestätigung" in st[EVM_GOOD.lower()][1], st[EVM_GOOD.lower()]   # wartet 15 min
    assert st[SOL_GOOD][0] == "alerted" or "Bestätigung" in st[SOL_GOOD][1]  # starker X-Hype = Schnellspur
    # 15 Minuten später: immer noch gut -> jetzt Call
    app.db.con.execute("UPDATE tokens SET passed_at=passed_at-1200, last_check=0 WHERE status='pending'")
    await app.scanner.run_cycle()
    st = {(r["address"]): (r["status"], r["reason"]) for r in app.db.con.execute("SELECT * FROM tokens")}
    print("\nErgebnisse Runde 2:", st)
    assert st[SOL_GOOD][0] == "alerted", st[SOL_GOOD]
    assert st[EVM_GOOD.lower()][0] == "alerted", st[EVM_GOOD.lower()]
    assert st[SOL_RUG][0] == "rejected" and "Freeze" in st[SOL_RUG][1]
    assert st[EVM_HONEY.lower()][0] == "rejected" and "HONEYPOT" in st[EVM_HONEY.lower()][1]

    # 3) Wallet-Tracking (erste Runde = nur merken, zweite = Meldung)
    app.db.add_wallet("evm", WALLET_EVM, "Smart Money")
    app.db.add_wallet("solana", WALLET_SOL, "Sol Wal")
    await app.wallets.run_cycle()
    calls.append("evm_wallet_round"); calls.append("sol_wallet_round")
    print("\n--- Wallet-Runde 2 (Meldungen erwartet) ---")
    await app.wallets.run_cycle()

    # 4) Manuelle Prüfung
    txt = await app.check_text(EVM_HONEY)
    assert "FINGER WEG" in txt, txt
    print("\n--- /check Honeypot ---\n" + m.strip_html(txt)[:600])
    await app.handle("/status")

    # 5) Neue Filter (v5)
    from bot.market import check_market, pair_metrics
    cfg = app.s.section("market")
    spike = dict(PAIRS[SOL_GOOD], priceChange={"m5": 35, "h1": 40, "h24": 50})
    assert any("Spike" in f for f in check_market(pair_metrics(spike), cfg))
    wash = dict(PAIRS[SOL_GOOD], volume={"m5": 1, "h1": 400000, "h24": 1})
    assert any("Wash" in f for f in check_market(pair_metrics(wash), cfg))

    # 6) X: Buzz + Promi-Posts
    ev = await app.scanner.evaluate("solana", SOL_GOOD, "Test", pair=PAIRS[SOL_GOOD])
    assert ev.x.get("accounts") == 8 and ev.x.get("big"), ev.x
    found = await app.xhype.kol_cycle()
    print("\nX-Kandidaten:", found)
    assert any(src.startswith("X-Trend: Cybercab") for _, _, src in found), found
    assert app.db.kv_get("x_feed")[0]["user"] == "elonmusk"

    # 7) Telegram-Erwähnungen
    await app.tghype.record("Alpha Calls", {SOL_GOOD})
    await app.tghype.record("Degen Gruppe", {SOL_GOOD, "NotAToken11111111111111111111111111"})
    b = app.tghype.buzz(SOL_GOOD)
    assert b["places"] == 2, b
    print(m.strip_html(await app.hot_text()))

    # 8) /set /get (bleibt gespeichert)
    print(app.set_cmd(["min_score", "70"]))
    assert app.s.get("min_score") == 70 and app.db.kv_get("overrides")["min_score"] == 70
    assert "Unbekannte" in app.set_cmd(["gibtsnicht", "1"])
    print(app.set_cmd(["market.max_chg_5m_pct", "25"]))
    assert app.s.section("market")["max_chg_5m_pct"] == 25
    assert "max_chg_5m_pct" in app.get_cmd(["market"])
    app.set_cmd(["reset", "min_score"])
    assert app.s.get("min_score") == 65
    print("\n✅ Alle Offline-Tests bestanden")


asyncio.run(main())
