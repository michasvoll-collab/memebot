"""Kleine SQLite-Datenbank für Zustand (gesehene Coins, Wallets, ...)."""
import json
import os
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS tokens (
    chain TEXT, address TEXT,
    first_seen REAL, last_check REAL,
    status TEXT DEFAULT 'pending',   -- pending | alerted | rejected
    source TEXT, reason TEXT, symbol TEXT,
    PRIMARY KEY (chain, address)
);
CREATE TABLE IF NOT EXISTS wallets (
    chain TEXT, address TEXT, label TEXT, added REAL,
    PRIMARY KEY (chain, address)
);
CREATE TABLE IF NOT EXISTS wallet_seen (
    chain TEXT, wallet TEXT, key TEXT, ts REAL,
    PRIMARY KEY (chain, wallet, key)
);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS calls (
    chain TEXT, address TEXT, symbol TEXT, name TEXT, url TEXT,
    call_ts REAL, call_mcap REAL, call_price REAL, score INTEGER,
    ath_mcap REAL, ath_ts REAL, last_mcap REAL, last_check REAL,
    best_milestone REAL DEFAULT 1, dead INTEGER DEFAULT 0,
    PRIMARY KEY (chain, address)
);
CREATE TABLE IF NOT EXISTS ai_calls (ts REAL, kind TEXT DEFAULT 'coin');
CREATE TABLE IF NOT EXISTS mentions (
    addr TEXT, source TEXT, place TEXT, ts REAL, weight REAL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_mentions ON mentions(addr, ts);
"""


class DB:
    def __init__(self, path: str):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.con = sqlite3.connect(path, check_same_thread=False)
        self.con.row_factory = sqlite3.Row
        self.con.executescript(SCHEMA)
        for tbl, col, typ in (("calls", "min_mcap", "REAL"), ("calls", "sl_notified", "INTEGER DEFAULT 0"),
                              ("ai_calls", "kind", "TEXT DEFAULT 'coin'"), ("calls", "hype", "TEXT"),
                              ("tokens", "passed_at", "REAL"), ("tokens", "passed_mcap", "REAL"),
                              ("calls", "source", "TEXT")):
            try:
                self.con.execute(f"ALTER TABLE {tbl} ADD COLUMN {col} {typ}")
            except sqlite3.OperationalError:
                pass  # Spalte gibt es schon
        self.con.commit()

    # ---------- Tokens ----------
    def add_candidate(self, chain, address, source, symbol="") -> bool:
        """True, wenn der Coin neu ist."""
        cur = self.con.execute(
            "INSERT OR IGNORE INTO tokens(chain,address,first_seen,last_check,source,symbol) VALUES(?,?,?,?,?,?)",
            (chain, address, time.time(), 0, source, symbol),
        )
        self.con.commit()
        return cur.rowcount > 0

    def due_candidates(self, recheck_sec, max_age_sec, limit=60):
        now = time.time()
        self.con.execute(
            "UPDATE tokens SET status='rejected', reason=COALESCE(reason,'')||' | Zeitlimit' "
            "WHERE status='pending' AND first_seen < ?", (now - max_age_sec,))
        self.con.commit()
        return self.con.execute(
            "SELECT * FROM tokens WHERE status='pending' AND last_check < ? ORDER BY last_check ASC LIMIT ?",
            (now - recheck_sec, limit)).fetchall()

    def mark(self, chain, address, status, reason=""):
        self.con.execute("UPDATE tokens SET status=?, reason=?, last_check=? WHERE chain=? AND address=?",
                         (status, reason, time.time(), chain, address))
        self.con.commit()

    def bump(self, chain, address):
        """Coin bald erneut prüfen (z. B. weil er gerade überall erwähnt wird)."""
        self.con.execute("UPDATE tokens SET last_check=0 WHERE chain=? AND address=? AND status='pending'",
                         (chain, address))
        self.con.commit()

    def set_passed(self, chain, address, mcap):
        self.con.execute("UPDATE tokens SET passed_at=?, passed_mcap=? WHERE chain=? AND address=?",
                         (time.time() if mcap is not None else None, mcap, chain, address))
        self.con.commit()

    def get_token(self, chain, address):
        return self.con.execute("SELECT * FROM tokens WHERE chain=? AND address=?", (chain, address)).fetchone()

    def stats(self):
        rows = self.con.execute("SELECT status, COUNT(*) n FROM tokens GROUP BY status").fetchall()
        return {r["status"]: r["n"] for r in rows}

    def cleanup(self, older_than_sec=7 * 86400):
        cutoff = time.time() - older_than_sec
        self.con.execute("DELETE FROM tokens WHERE first_seen < ? AND status != 'alerted'", (cutoff,))
        self.con.execute("DELETE FROM wallet_seen WHERE ts < ?", (time.time() - 30 * 86400,))
        self.con.execute("DELETE FROM ai_calls WHERE ts < ?", (time.time() - 86400,))
        self.con.execute("DELETE FROM mentions WHERE ts < ?", (time.time() - 3 * 86400,))
        self.con.commit()

    # ---------- Calls (gemeldete Coins + Performance) ----------
    def add_call(self, chain, address, m, score, source="", hype=""):
        now = time.time()
        self.con.execute(
            "INSERT OR IGNORE INTO calls(chain,address,symbol,name,url,call_ts,call_mcap,call_price,score,"
            "ath_mcap,ath_ts,last_mcap,last_check,source,hype) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (chain, address, m["symbol"], m["name"], m.get("url", ""), now, m["mcap"], m["price"], score,
             m["mcap"], now, m["mcap"], now, source, hype))
        self.con.commit()

    def active_calls(self, max_age_days=14):
        return self.con.execute("SELECT * FROM calls WHERE call_ts > ? ORDER BY call_ts DESC",
                                (time.time() - max_age_days * 86400,)).fetchall()

    def all_calls(self):
        return self.con.execute("SELECT * FROM calls ORDER BY call_ts").fetchall()

    def recent_calls(self, limit=15):
        return self.con.execute("SELECT * FROM calls ORDER BY call_ts DESC LIMIT ?", (limit,)).fetchall()

    def calls_since(self, ts):
        return self.con.execute("SELECT * FROM calls WHERE call_ts > ? ORDER BY call_ts", (ts,)).fetchall()

    def update_call(self, chain, address, **fields):
        cols = ", ".join(f"{k}=?" for k in fields)
        self.con.execute(f"UPDATE calls SET {cols} WHERE chain=? AND address=?", (*fields.values(), chain, address))
        self.con.commit()

    # ---------- Hype-Erwähnungen (X, Telegram) ----------
    def add_mention(self, addr, source, place, weight=1.0):
        self.con.execute("INSERT INTO mentions VALUES(?,?,?,?,?)", (addr, source, place, time.time(), weight))
        self.con.commit()

    def mention_stats(self, addr, minutes=60, source=None):
        q = "SELECT COUNT(*) n, COUNT(DISTINCT place) places, COALESCE(SUM(weight),0) w FROM mentions WHERE addr=? AND ts>?"
        args = [addr, time.time() - minutes * 60]
        if source:
            q += " AND source=?"
            args.append(source)
        r = self.con.execute(q, args).fetchone()
        return {"n": r["n"], "places": r["places"], "weight": r["w"]}

    def top_mentions(self, minutes=60, limit=10):
        return self.con.execute(
            "SELECT addr, COUNT(*) n, COUNT(DISTINCT place) places FROM mentions WHERE ts>? "
            "GROUP BY addr ORDER BY places DESC, n DESC LIMIT ?", (time.time() - minutes * 60, limit)).fetchall()

    # ---------- Wallets ----------
    def add_wallet(self, chain, address, label):
        self.con.execute("INSERT OR REPLACE INTO wallets VALUES(?,?,?,?)", (chain, address, label, time.time()))
        self.con.commit()

    def remove_wallet(self, address) -> int:
        cur = self.con.execute("DELETE FROM wallets WHERE lower(address)=lower(?) OR label=?", (address, address))
        self.con.commit()
        return cur.rowcount

    def wallets(self):
        return self.con.execute("SELECT * FROM wallets ORDER BY added").fetchall()

    def wallet_seen(self, chain, wallet, key) -> bool:
        return self.con.execute("SELECT 1 FROM wallet_seen WHERE chain=? AND wallet=? AND key=?",
                                (chain, wallet, key)).fetchone() is not None

    def mark_wallet_seen(self, chain, wallet, key):
        self.con.execute("INSERT OR IGNORE INTO wallet_seen VALUES(?,?,?,?)", (chain, wallet, key, time.time()))
        self.con.commit()

    # ---------- Key/Value ----------
    def kv_get(self, k, default=None):
        r = self.con.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return json.loads(r["v"]) if r else default

    def kv_set(self, k, v):
        self.con.execute("INSERT OR REPLACE INTO kv VALUES(?,?)", (k, json.dumps(v)))
        self.con.commit()

    # ---------- KI-Kostenbremse ----------
    def ai_calls_last_hour(self, kind=None) -> int:
        if kind:
            return self.con.execute("SELECT COUNT(*) FROM ai_calls WHERE ts > ? AND kind=?",
                                    (time.time() - 3600, kind)).fetchone()[0]
        return self.con.execute("SELECT COUNT(*) FROM ai_calls WHERE ts > ?", (time.time() - 3600,)).fetchone()[0]

    def log_ai_call(self, kind="coin"):
        self.con.execute("INSERT INTO ai_calls(ts, kind) VALUES(?, ?)", (time.time(), kind))
        self.con.commit()
