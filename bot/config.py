import os
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


class Settings:
    def __init__(self, config_path: str | None = None):
        load_dotenv(ROOT / ".env")
        path = Path(config_path or os.getenv("MEMEBOT_CONFIG", ROOT / "config.yaml"))
        with open(path, encoding="utf-8") as f:
            self.cfg = yaml.safe_load(f) or {}

        self.telegram_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        self.telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
        self.anthropic_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
        self.etherscan_key = os.getenv("ETHERSCAN_API_KEY", "").strip()
        self.solana_rpc = os.getenv("SOLANA_RPC_URL", "").strip() or "https://api.mainnet-beta.solana.com"
        self.goplus_key = os.getenv("GOPLUS_API_KEY", "").strip()
        self.dry_run = os.getenv("DRY_RUN", "0").strip() in ("1", "true", "yes")
        self.db_path = os.getenv("MEMEBOT_DB", str(ROOT / "data" / "memebot.db"))
        self.x_key = os.getenv("TWITTERAPI_KEY", "").strip()
        self.tg_api_id = os.getenv("TG_API_ID", "").strip()
        self.tg_api_hash = os.getenv("TG_API_HASH", "").strip()
        self.overrides: dict = {}   # per /set geänderte Werte, z. B. {"market.min_age_minutes": 20}

    def get(self, *keys, default=None):
        dotted = ".".join(keys)
        if dotted in self.overrides:
            return self.overrides[dotted]
        if len(keys) == 1 and isinstance(self.cfg.get(keys[0]), dict):
            return self.section(keys[0])
        cur = self.cfg
        for k in keys:
            if not isinstance(cur, dict) or k not in cur:
                return default
            cur = cur[k]
        return cur

    def section(self, name) -> dict:
        """Abschnitt aus config.yaml inkl. /set-Änderungen."""
        base = dict(self.cfg.get(name) or {})
        for k, v in self.overrides.items():
            if k.startswith(name + "."):
                base[k.split(".", 1)[1]] = v
        return base

    def set_value(self, dotted: str, raw: str):
        """Wert per /set ändern. Gibt den umgewandelten Wert zurück oder wirft ValueError."""
        parts = dotted.split(".")
        cur = self.cfg
        for p in parts:
            if not isinstance(cur, dict) or p not in cur:
                raise ValueError(f"Unbekannte Einstellung: {dotted}")
            cur = cur[p]
        if isinstance(cur, bool):
            val = raw.lower() in ("1", "true", "ja", "an", "on", "yes")
        elif isinstance(cur, int):
            val = int(float(raw))
        elif isinstance(cur, float):
            val = float(raw)
        elif isinstance(cur, list):
            val = [x.strip() for x in raw.split(",") if x.strip()]
        elif isinstance(cur, dict):
            raise ValueError("Bitte einen einzelnen Wert angeben, z. B. market.min_age_minutes")
        else:
            val = raw
        self.overrides[dotted] = val
        return val

    @property
    def chains(self) -> list[str]:
        from .chains import CHAINS
        return [c for c in (self.overrides.get("chains") or self.cfg.get("chains", list(CHAINS))) if c in CHAINS]
