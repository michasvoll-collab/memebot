"""Telegram-Nachrichten formatieren."""
from html import escape

from .chains import CHAINS


def usd(v) -> str:
    if v is None:
        return "?"
    v = float(v)
    if v >= 1e9:
        return f"${v / 1e9:.2f}B"
    if v >= 1e6:
        return f"${v / 1e6:.2f}M"
    if v >= 1e3:
        return f"${v / 1e3:.1f}K"
    return f"${v:,.0f}"


def price(v) -> str:
    if not v:
        return "?"
    return f"${v:.10f}".rstrip("0") if v < 0.01 else f"${v:,.4f}"


def age(m) -> str:
    if m is None:
        return "?"
    if m < 60:
        return f"{m:.0f} min"
    if m < 48 * 60:
        return f"{m / 60:.1f} h"
    return f"{m / 1440:.0f} Tage"


def pct(v) -> str:
    return f"{'+' if v > 0 else ''}{v:.0f}%"


def links(chain, addr, m) -> str:
    out = [f'<a href="{m.get("url") or f"https://dexscreener.com/{CHAINS[chain]["dexscreener"]}/{addr}"}">DexScreener</a>']
    if chain == "solana":
        out += [f'<a href="https://rugcheck.xyz/tokens/{addr}">RugCheck</a>',
                f'<a href="https://gmgn.ai/sol/token/{addr}">GMGN</a>']
    else:
        out += [f'<a href="https://honeypot.is/?address={addr}">Honeypot.is</a>',
                f'<a href="https://gopluslabs.io/token-security/{CHAINS[chain]["goplus"]}/{addr}">GoPlus</a>']
    out.append(f'<a href="{CHAINS[chain]["explorer"].format(addr)}">Explorer</a>')
    for s in (m.get("socials") or [])[:3]:
        if s.get("url", "").startswith("http"):
            out.append(f'<a href="{escape(s["url"])}">{escape(str(s["type"]).capitalize())}</a>')
    for w in (m.get("websites") or [])[:1]:
        out.append(f'<a href="{escape(w)}">Website</a>')
    return " | ".join(out)


def bar(score: int) -> str:
    full = round(score / 10)
    return "🟩" * full + "⬜" * (10 - full)


def coin_message(ev, header="🚀 NEUER COIN – alle Kriterien erfüllt", is_call=True) -> str:
    m, sec, chain, addr = ev.metrics, ev.security, ev.chain, ev.address
    f = sec.facts if sec else {}
    lines = [
        f"<b>{header}</b>",
        "",
        f"<b>{escape(m['name'])}</b> (${escape(m['symbol'])}) · {CHAINS[chain]['name']} · {escape(m['dex'])}",
        f"<code>{addr}</code>",
        "",
        (f"📍 <b>Call bei MC {usd(m['mcap'])}</b> · Preis {price(m['price'])}" if is_call
         else f"📍 Aktuelle MC {usd(m['mcap'])} · Preis {price(m['price'])}"),
        f"⭐ Gesamt: <b>{ev.total}/100</b> {bar(ev.total)}",
        f"   Sicherheit {ev.sec_score} · Markt {ev.market_score} · Hype {ev.hype_score}",
        "",
        f"💰 Preis {price(m['price'])} · MCap {usd(m['mcap'])} · Liq {usd(m['liquidity'])}",
        f"📊 Vol 1h {usd(m['vol_1h'])} · 24h {usd(m['vol_24h'])} · Trades 1h {m['txns_1h']:.0f} "
        f"(🟢{m['buys_1h']:.0f}/🔴{m['sells_1h']:.0f})",
        f"📈 5m {pct(m['chg_5m'])} · 1h {pct(m['chg_1h'])} · 24h {pct(m['chg_24h'])} · Alter {age(m['age_min'])}",
    ]
    if sec:
        sec_bits = []
        if f.get("buy_tax") is not None:
            sec_bits.append(f"Tax {f.get('buy_tax', 0):.0f}/{f.get('sell_tax', 0):.0f}%")
        if f.get("lp_locked_pct") is not None:
            sec_bits.append(f"LP gesperrt {f['lp_locked_pct']:.0f}%")
        if f.get("top10_pct") is not None:
            sec_bits.append(f"Top10 {f['top10_pct']:.0f}%")
        if f.get("holders"):
            sec_bits.append(f"{f['holders']:,} Holder")
        lines += ["", "🛡 " + " · ".join(sec_bits)]
        if sec.hard_fails:
            lines += ["❌ " + escape(x) for x in sec.hard_fails[:6]]
        if sec.positives:
            lines.append("✅ " + escape(", ".join(sec.positives[:5])))
        if sec.warnings:
            lines.append("⚠️ " + escape(", ".join(sec.warnings[:5])))
    if ev.market_fails:
        lines += ["", "📉 Markt-Kriterien nicht erfüllt:"] + ["• " + escape(x) for x in ev.market_fails[:6]]
    hx, ht = getattr(ev, "x", {}) or {}, getattr(ev, "tgb", {}) or {}
    if hx or ht:
        lines.append("")
    if hx:
        lines.append(f"🐦 X letzte Stunde: <b>{hx.get('tweets', 0)}{'+' if hx.get('full') else ''} Posts</b> von "
                     f"{hx.get('accounts', 0)} Accounts · Reichweite {usd(hx.get('reach', 0))[1:]}"
                     + (f"\n   Große Accounts: {escape(', '.join(hx['big']))}" if hx.get("big") else ""))
    if ht:
        lines.append(f"✈️ Telegram letzte Stunde: <b>{ht.get('places', 0)} Kanäle</b>, {ht.get('n', 0)} Erwähnungen")
    if ev.trend:
        lines += ["", f"📰 Passt zum Trend <b>{escape(ev.trend['keyword'])}</b>: {escape(ev.trend.get('headline', ''))[:160]}"]
    if ev.news:
        lines += ["", "🗞 News:"] + [f"• {escape(n)[:140]}" for n in ev.news[:3]]
    if ev.ai_text:
        lines += ["", escape(ev.ai_text)]
    lines += ["", f"🔎 Quelle: {escape(ev.source)}", links(chain, addr, m),
              "", "<i>Keine Anlageberatung – nur Geld einsetzen, das du verlieren kannst.</i>"]
    return "\n".join(lines)
