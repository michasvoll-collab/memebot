"""Entscheidung (KAUFEN / WARTEN / ÜBERSPRINGEN) + Trade-Plan mit festen Risiko-Regeln."""
from html import escape


def usd(v) -> str:
    v = float(v or 0)
    if v >= 1e6:
        return f"${v / 1e6:.2f}M"
    if v >= 1e3:
        return f"${v / 1e3:.0f}K"
    return f"${v:,.0f}"


def decide(ev, settings) -> tuple[str, str]:
    """Kurze Entscheidung + Begründung für einen Coin."""
    if getattr(ev, "override", None):
        return ev.override
    m, sec = ev.metrics, ev.security
    if not ev.found or sec is None:
        return "⏭️ ÜBERSPRINGEN", "keine verlässlichen Daten"
    if sec.fatal:
        return "⏭️ ÜBERSPRINGEN", "Sicherheits-K.O.: " + "; ".join(sec.hard_fails[:2])
    if not sec.passed:
        return "⏭️ ÜBERSPRINGEN", "Sicherheit nicht bestanden: " + "; ".join(sec.hard_fails[:2])
    smart = len(getattr(ev, "smart", []) or [])
    if ev.market_fails and not smart:
        return "⏭️ ÜBERSPRINGEN", ev.market_fails[0]
    wait = []
    if m["chg_5m"] > 12:
        wait.append(f"gerade +{m['chg_5m']:.0f} % in 5 min – Rücksetzer abwarten")
    if m["liquidity"] and m["vol_1h"] / m["liquidity"] > 3:
        wait.append("sehr hohes Volumen im Verhältnis zur Liquidität")
    if m["buy_sell_ratio"] < 1:
        wait.append("gerade mehr Verkäufe als Käufe")
    if ev.total < settings.get("min_score", default=65):
        wait.append(f"Score nur {ev.total}")
    if wait:
        return "⏳ WARTEN", wait[0]
    why = []
    if smart:
        why.append(f"{smart} Smart-Money-Wallets drin")
    if ev.hype_score >= 70:
        why.append(f"starker Hype ({ev.hype_score})")
    if ev.sec_score >= 85:
        why.append("Sicherheit sehr gut")
    if ev.market_score >= 75:
        why.append("gesunder Markt")
    return "✅ KAUFEN", ", ".join(why) or f"alle Kriterien erfüllt (Score {ev.total})"


def stake_for(ev, r) -> tuple[float, str]:
    """Einsatz nach Signal-Stärke: schwach = klein, stark = voll."""
    smart = len(getattr(ev, "smart", []) or [])
    if smart >= 3 or ev.total >= r.get("score_high", 85):
        return r.get("stake_high", 20), "starkes Signal"
    if ev.total >= r.get("score_mid", 75):
        return r.get("stake_mid", 10), "mittleres Signal"
    return r.get("stake_low", 5), "schwächeres Signal"


def plan_lines(ev, settings) -> list[str]:
    """Trade-Plan in Market-Cap-Zahlen + feste Risiko-Regeln."""
    r = settings.section("risk") or {}
    mc = ev.metrics.get("mcap") or 0
    if not mc:
        return []
    stake, level = stake_for(ev, r)
    tp1, tp2 = r.get("take_profit_x", 2), r.get("target2_x", 5)
    sl = r.get("stop_loss_pct", 50)
    chase = r.get("max_entry_above_pct", 30)
    return [
        "<b>📋 Trade-Plan</b>",
        f"• Einstieg jetzt bei MC {usd(mc)} – nicht mehr kaufen über {usd(mc * (1 + chase / 100))}",
        f"• Ziel 1: MC {usd(mc * tp1)} ({tp1:g}x) → Einsatz rausnehmen",
        f"• Ziel 2: MC {usd(mc * tp2)} ({tp2:g}x) → Rest teilweise verkaufen",
        f"• Stop-Loss: MC {usd(mc * (1 - sl / 100))} (−{sl:g} %) → komplett raus",
        f"💶 Einsatz: <b>{stake:g} €</b> ({level}) · nie mehr, als du verlieren kannst",
    ]


def decision_block(ev, settings, with_plan=True) -> list[str]:
    d, why = decide(ev, settings)
    lines = [f"<b>🎯 Entscheidung: {d}</b>", f"   {escape(why)}"]
    if with_plan and d.startswith("✅"):
        lines += [""] + plan_lines(ev, settings)
    return lines
