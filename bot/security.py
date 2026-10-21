"""Honeypot- / Rug-Pull-Checks.

EVM  (Ethereum, Base, BSC, Arbitrum): GoPlus Security + Honeypot.is (Kauf/Verkauf-Simulation)
Solana:                              RugCheck.xyz + GoPlus Solana
"""
import logging
from dataclasses import dataclass, field

from .chains import BURN_ADDRESSES, CHAINS

log = logging.getLogger("security")
GOPLUS = "https://api.gopluslabs.io/api/v1"


@dataclass
class SecurityResult:
    hard_fails: list[str] = field(default_factory=list)   # K.O.-Kriterien
    warnings: list[str] = field(default_factory=list)     # Abzug bei Punkten
    positives: list[str] = field(default_factory=list)
    retry_later: bool = False      # Daten noch nicht verfügbar (Coin zu neu) -> später nochmal
    fatal: bool = False            # K.O. für immer (Honeypot etc.) – sonst später erneut prüfen
    score: int = 100               # 0..100 Sicherheits-Punkte
    facts: dict = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return not self.hard_fails and not self.retry_later

    def fail(self, msg, fatal=True):
        self.hard_fails.append(msg)
        self.fatal = self.fatal or fatal

    def warn(self, msg, minus=5):
        self.warnings.append(msg)
        self.score -= minus

    def good(self, msg):
        self.positives.append(msg)


def _f(v, default=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _is1(v) -> bool:
    return str(v) == "1"


class SecurityChecker:
    def __init__(self, http, settings):
        self.http = http
        self.s = settings

    @property
    def c(self):
        return self.s.section("security")

    async def check(self, chain: str, address: str, pair_address: str = "") -> SecurityResult:
        if chain == "solana":
            res = await self._solana(address, pair_address)
        else:
            res = await self._evm(chain, address, pair_address)
        if res.fatal:
            res.score = 0
        elif res.hard_fails:
            res.score = min(res.score, 40)
        res.score = max(0, min(100, res.score))
        return res

    # ================= EVM =================
    async def _evm(self, chain, address, pair_address) -> SecurityResult:
        r = SecurityResult()
        cid = CHAINS[chain]["goplus"]
        headers = {"Authorization": self.s.goplus_key} if self.s.goplus_key else {}
        gp = await self.http.get_json(f"{GOPLUS}/token_security/{cid}?contract_addresses={address}", headers=headers)
        data = ((gp or {}).get("result") or {}).get(address.lower())

        hp_id = CHAINS[chain]["honeypot"]
        hp = None
        if hp_id:
            q = f"address={address}&chainID={hp_id}" + (f"&pair={pair_address}" if pair_address else "")
            hp = await self.http.get_json(f"https://api.honeypot.is/v2/IsHoneypot?{q}")

        if not data and not hp:
            r.retry_later = True
            r.fail("Noch keine Sicherheitsdaten verfügbar", fatal=False)
            return r

        # ---- Honeypot.is Simulation ----
        if hp:
            hres = hp.get("honeypotResult") or {}
            sim = hp.get("simulationResult") or {}
            if hres.get("isHoneypot"):
                r.fail(f"🍯 HONEYPOT (Simulation): {hres.get('honeypotReason', 'Verkauf nicht möglich')}")
            elif hp.get("simulationSuccess"):
                r.good("Kauf/Verkauf-Simulation erfolgreich")
            else:
                r.warn("Kauf/Verkauf-Simulation fehlgeschlagen", 10)
            bt, st = _f(sim.get("buyTax")), _f(sim.get("sellTax"))
            if bt is not None:
                r.facts["buy_tax"] = bt
            if st is not None:
                r.facts["sell_tax"] = st
            risk = ((hp.get("summary") or {}).get("risk") or "").lower()
            if risk in ("high", "very_high"):
                r.warn(f"Honeypot.is Risiko: {risk}", 15)
            for flag in (hp.get("summary") or {}).get("flags") or hp.get("flags") or []:
                desc = flag.get("description") if isinstance(flag, dict) else str(flag)
                sev = (flag.get("severity") if isinstance(flag, dict) else "") or ""
                if sev.lower() in ("high", "critical"):
                    r.warn(f"Flag: {desc}", 10)

        # ---- GoPlus Vertragsanalyse ----
        if data:
            if _is1(data.get("is_honeypot")):
                r.fail("🍯 HONEYPOT laut GoPlus")
            if _is1(data.get("cannot_sell_all")):
                r.fail("Man kann nicht alles verkaufen")
            if _is1(data.get("honeypot_with_same_creator")):
                r.fail("Ersteller hat schon Honeypots gebaut")
            if _is1(data.get("is_airdrop_scam")) or _is1(data.get("fake_token")):
                r.fail("Als Scam/Fake-Token markiert")
            if _is1(data.get("selfdestruct")):
                r.fail("Vertrag kann sich selbst zerstören")
            if _is1(data.get("owner_change_balance")):
                r.fail("Owner kann Guthaben ändern")
            if _is1(data.get("hidden_owner")):
                r.fail("Versteckter Owner")
            if _is1(data.get("can_take_back_ownership")):
                r.fail("Owner kann Besitz zurückholen")
            if _is1(data.get("transfer_pausable")):
                r.warn("Handel kann pausiert werden", 15)
            if _is1(data.get("is_blacklisted")):
                r.warn("Blacklist-Funktion vorhanden", 15)
            if _is1(data.get("slippage_modifiable")) or _is1(data.get("personal_slippage_modifiable")):
                r.warn("Steuern können geändert werden", 15)
            if _is1(data.get("trading_cooldown")):
                r.warn("Trading-Cooldown", 5)
            if _is1(data.get("external_call")):
                r.warn("Externe Aufrufe im Vertrag", 5)
            if _is1(data.get("is_proxy")):
                r.warn("Proxy-Vertrag (Code änderbar)", 15)

            if self.c.get("require_open_source", True) and str(data.get("is_open_source")) == "0":
                r.fail("Vertrag nicht verifiziert (Code geheim)")
            elif _is1(data.get("is_open_source")):
                r.good("Vertrag verifiziert")

            owner = (data.get("owner_address") or "").lower()
            renounced = owner in ("", *BURN_ADDRESSES)
            mintable = _is1(data.get("is_mintable"))
            if renounced:
                r.good("Ownership abgegeben")
            if mintable and not renounced:
                msg = "Owner kann neue Token minten"
                r.fail(msg) if self.c.get("require_renounced_or_no_mint", True) else r.warn(msg, 20)

            # Steuern (GoPlus: 0.05 = 5 %)
            if "buy_tax" not in r.facts and _f(data.get("buy_tax")) is not None:
                r.facts["buy_tax"] = _f(data.get("buy_tax")) * 100
            if "sell_tax" not in r.facts and _f(data.get("sell_tax")) is not None:
                r.facts["sell_tax"] = _f(data.get("sell_tax")) * 100

            # Liquidität gesperrt?
            lp = data.get("lp_holders") or []
            if lp:
                locked = sum(_f(h.get("percent"), 0) for h in lp
                             if _is1(h.get("is_locked")) or (h.get("address") or "").lower() in BURN_ADDRESSES)
                r.facts["lp_locked_pct"] = locked * 100
            else:
                r.warn("LP-Sperre nicht prüfbar (z. B. Uniswap V3/V4)", 10)

            # Holder-Verteilung
            excluded = {a.lower() for a in BURN_ADDRESSES} | {(pair_address or "").lower()}
            excluded |= {(h.get("address") or "").lower() for h in lp}
            holders = [h for h in data.get("holders") or []
                       if (h.get("address") or "").lower() not in excluded and not _is1(h.get("is_locked"))]
            pcts = sorted((_f(h.get("percent"), 0) * 100 for h in holders), reverse=True)
            r.facts["top10_pct"] = sum(pcts[:10])
            r.facts["top1_pct"] = pcts[0] if pcts else 0
            r.facts["creator_pct"] = (_f(data.get("creator_percent"), 0) or 0) * 100
            r.facts["holders"] = int(_f(data.get("holder_count"), 0) or 0)

        self._common_limits(r)
        return r

    # ================= Solana =================
    async def _solana(self, mint, pair_address) -> SecurityResult:
        r = SecurityResult()
        rc = await self.http.get_json(f"https://api.rugcheck.xyz/v1/tokens/{mint}/report")
        headers = {"Authorization": self.s.goplus_key} if self.s.goplus_key else {}
        gp = await self.http.get_json(f"{GOPLUS}/solana/token_security?contract_addresses={mint}", headers=headers)
        g = ((gp or {}).get("result") or {}).get(mint)

        if not rc and not g:
            r.retry_later = True
            r.fail("Noch keine Sicherheitsdaten verfügbar", fatal=False)
            return r

        if rc:
            if rc.get("rugged"):
                r.fail("☠️ RugCheck: Coin wurde bereits gerugged")
            tok = rc.get("token") or {}
            mint_auth = rc.get("mintAuthority", tok.get("mintAuthority"))
            freeze_auth = rc.get("freezeAuthority", tok.get("freezeAuthority"))
            if mint_auth:
                msg = "Mint-Authority aktiv (Ersteller kann neue Token drucken)"
                r.fail(msg) if self.c.get("solana_require_no_mint_authority", True) else r.warn(msg, 25)
            else:
                r.good("Mint-Authority entfernt")
            if freeze_auth:
                msg = "Freeze-Authority aktiv (deine Token können eingefroren werden = Honeypot)"
                r.fail(msg) if self.c.get("solana_require_no_freeze_authority", True) else r.warn(msg, 25)
            else:
                r.good("Freeze-Authority entfernt")

            score = _f(rc.get("score"), 0)
            r.facts["rugcheck_score"] = score
            if score > self.c.get("solana_max_rugcheck_score", 1000):
                r.fail(f"RugCheck-Risiko zu hoch ({score:.0f})")
            for risk in rc.get("risks") or []:
                lvl = (risk.get("level") or "").lower()
                name = risk.get("name", "?")
                if lvl == "danger":
                    r.warn(f"RugCheck: {name}", 15)
                elif lvl == "warn":
                    r.warn(f"RugCheck: {name}", 5)

            tf = _f((rc.get("transferFee") or {}).get("pct"), 0) or 0
            r.facts["buy_tax"] = r.facts["sell_tax"] = tf

            # LP-Sperre: höchster Wert der liquidesten Märkte
            markets = rc.get("markets") or []
            if markets:
                best = max(markets, key=lambda m: _f((m.get("lp") or {}).get("quoteUSD"), 0) or 0)
                r.facts["lp_locked_pct"] = _f((best.get("lp") or {}).get("lpLockedPct"), 0)
                r.facts["market_type"] = mt = (best.get("marketType") or "").lower()
                # pump.fun-Bonding-Curve: es gibt keine LP-Token, die abgezogen werden können
                if "pump_fun" in mt and "amm" not in mt:
                    r.facts["lp_locked_pct"] = 100.0
                    r.good("Noch auf pump.fun Bonding Curve (keine LP zum Abziehen)")

            known = rc.get("knownAccounts") or {}
            market_keys = {m.get("pubkey") for m in markets} | {pair_address}
            pcts = []
            for h in rc.get("topHolders") or []:
                owner = h.get("owner") or h.get("address")
                k = known.get(owner) or known.get(h.get("address")) or {}
                if owner in market_keys or (k.get("type") or "").upper() in ("AMM", "LOCKER", "BURN"):
                    continue
                if owner in BURN_ADDRESSES:
                    continue
                pcts.append(_f(h.get("pct"), 0) or 0)
            pcts.sort(reverse=True)
            r.facts["top10_pct"] = sum(pcts[:10])
            r.facts["top1_pct"] = pcts[0] if pcts else 0
            r.facts["holders"] = int(_f(rc.get("totalHolders"), 0) or 0)
            insiders = rc.get("graphInsidersDetected") or 0
            r.facts["insiders"] = int(insiders or 0)
            if insiders and insiders >= self.c.get("max_insiders", 1e9):
                r.fail(fatal=False, msg=f"{insiders} Insider-Wallets (Bundles) – zu viele")
            elif insiders and insiders > 3:
                r.warn(f"{insiders} Insider-Wallets erkannt (Bundles)", 15)
            dev_tokens = rc.get("creatorTokens") or []
            if isinstance(dev_tokens, list) and len(dev_tokens) >= self.c.get("max_dev_tokens", 1e9):
                r.warn(f"Ersteller hat schon {len(dev_tokens)} Coins gemacht (Serien-Dev)", 15)
            cb = _f(rc.get("creatorBalance"), 0) or 0
            supply = _f(tok.get("supply"), 0) or 0
            if cb and supply:
                r.facts["creator_pct"] = cb / supply * 100
            if ((rc.get("tokenMeta") or {}).get("mutable")):
                r.warn("Metadaten änderbar", 3)

        if g:
            if _is1((g.get("balance_mutable_authority") or {}).get("status")):
                r.fail("Guthaben können vom Ersteller geändert werden")
            if _is1((g.get("closable") or {}).get("status")):
                r.warn("Token-Konten schließbar", 10)
            if _is1(g.get("non_transferable")):
                r.fail("Token nicht übertragbar (Honeypot)")
            if g.get("transfer_hook"):
                r.warn("Transfer-Hook vorhanden (kann Verkäufe blockieren)", 20)
            if not rc:
                if _is1((g.get("mintable") or {}).get("status")):
                    r.fail("Mint-Authority aktiv")
                if _is1((g.get("freezable") or {}).get("status")):
                    r.fail("Freeze-Authority aktiv (Honeypot-Gefahr)")

        self._common_limits(r)
        return r

    # ================= Gemeinsame Grenzwerte =================
    def _common_limits(self, r: SecurityResult):
        c, f = self.c, r.facts
        if f.get("buy_tax") is not None and f["buy_tax"] > c.get("max_buy_tax_pct", 10):
            r.fail(f"Kauf-Steuer {f['buy_tax']:.1f} %")
        if f.get("sell_tax") is not None and f["sell_tax"] > c.get("max_sell_tax_pct", 10):
            r.fail(f"Verkaufs-Steuer {f['sell_tax']:.1f} %")
        if f.get("lp_locked_pct") is not None:
            if f["lp_locked_pct"] < c.get("min_lp_locked_pct", 0):
                r.fail(fatal=False, msg=f"Nur {f['lp_locked_pct']:.0f} % Liquidität gesperrt → Rug-Pull-Gefahr")
            else:
                r.good(f"{f['lp_locked_pct']:.0f} % Liquidität gesperrt/verbrannt")
        if f.get("top10_pct") is not None and f["top10_pct"] > c.get("max_top10_holders_pct", 100):
            r.fail(fatal=False, msg=f"Top-10-Wallets halten {f['top10_pct']:.0f} %")
        if f.get("top1_pct") is not None and f["top1_pct"] > c.get("max_single_holder_pct", 100):
            r.fail(fatal=False, msg=f"Eine Wallet hält {f['top1_pct']:.0f} %")
        if f.get("creator_pct") and f["creator_pct"] > c.get("max_creator_pct", 100):
            r.fail(fatal=False, msg=f"Ersteller hält noch {f['creator_pct']:.1f} %")
