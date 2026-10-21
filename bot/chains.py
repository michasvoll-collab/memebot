"""Zuordnung der Chain-Namen zu den IDs der verschiedenen Datenquellen."""

CHAINS = {
    "solana": {
        "name": "Solana", "dexscreener": "solana", "gecko": "solana",
        "goplus": None, "honeypot": None, "evm_chainid": None, "blockscout": None,
        "explorer": "https://solscan.io/token/{}",
    },
    "ethereum": {
        "name": "Ethereum", "dexscreener": "ethereum", "gecko": "eth",
        "goplus": "1", "honeypot": 1, "evm_chainid": 1,
        "blockscout": "https://eth.blockscout.com",
        "explorer": "https://etherscan.io/token/{}",
    },
    "base": {
        "name": "Base", "dexscreener": "base", "gecko": "base",
        "goplus": "8453", "honeypot": 8453, "evm_chainid": 8453,
        "blockscout": "https://base.blockscout.com",
        "explorer": "https://basescan.org/token/{}",
    },
    "bsc": {
        "name": "BNB Chain", "dexscreener": "bsc", "gecko": "bsc",
        "goplus": "56", "honeypot": 56, "evm_chainid": 56,
        "blockscout": None,
        "explorer": "https://bscscan.com/token/{}",
    },
    "arbitrum": {
        "name": "Arbitrum", "dexscreener": "arbitrum", "gecko": "arbitrum",
        "goplus": "42161", "honeypot": None, "evm_chainid": 42161,
        "blockscout": "https://arbitrum.blockscout.com",
        "explorer": "https://arbiscan.io/token/{}",
    },
}

DEX_TO_CHAIN = {v["dexscreener"]: k for k, v in CHAINS.items()}
GECKO_TO_CHAIN = {v["gecko"]: k for k, v in CHAINS.items()}
EVM_CHAINS = [k for k, v in CHAINS.items() if v["evm_chainid"]]

# Adressen, die bei Holder-Analysen nicht als "Wal" zählen (Burn/Null)
BURN_ADDRESSES = {
    "0x000000000000000000000000000000000000dead",
    "0x0000000000000000000000000000000000000000",
    "1nc1nerator11111111111111111111111111111111",
}


def is_evm_address(addr: str) -> bool:
    return addr.startswith("0x") and len(addr) == 42


def is_solana_address(addr: str) -> bool:
    import re
    return bool(re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{32,44}", addr))


def norm(chain: str, addr: str) -> str:
    """EVM-Adressen klein schreiben, Solana bleibt case-sensitiv."""
    return addr.lower() if chain != "solana" else addr
