import json, sqlite3, os
from pathlib import Path
from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS ledger(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, kind TEXT NOT NULL,
  symbol TEXT, market TEXT, qty INTEGER, price_local REAL, ccy TEXT, fx REAL,
  gross_myr REAL, fees_myr REAL, slippage_myr REAL, cash_delta_myr REAL NOT NULL,
  realized_pnl_myr REAL, order_id INTEGER, uniq TEXT UNIQUE, note TEXT);
CREATE TRIGGER IF NOT EXISTS ledger_no_update BEFORE UPDATE ON ledger BEGIN SELECT RAISE(ABORT,'ledger is append-only'); END;
CREATE TRIGGER IF NOT EXISTS ledger_no_delete BEFORE DELETE ON ledger BEGIN SELECT RAISE(ABORT,'ledger is append-only'); END;
CREATE TABLE IF NOT EXISTS decisions(
  id INTEGER PRIMARY KEY AUTOINCREMENT, decision_key TEXT UNIQUE NOT NULL, ts TEXT NOT NULL,
  scan_date TEXT, symbol TEXT, market TEXT, algo_version TEXT, data_mode TEXT,
  decision TEXT, score REAL, band TEXT, snapshot TEXT, scores TEXT, checks TEXT, plan TEXT, explanation TEXT);
CREATE TRIGGER IF NOT EXISTS dec_no_update BEFORE UPDATE ON decisions BEGIN SELECT RAISE(ABORT,'decisions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS dec_no_delete BEFORE DELETE ON decisions BEGIN SELECT RAISE(ABORT,'decisions are immutable'); END;
CREATE TABLE IF NOT EXISTS orders(
  id INTEGER PRIMARY KEY AUTOINCREMENT, decision_key TEXT UNIQUE NOT NULL, decision_id INTEGER,
  created_ts TEXT NOT NULL, updated_ts TEXT, symbol TEXT, market TEXT, side TEXT, qty INTEGER,
  ref_price REAL, stop REAL, target REAL, status TEXT NOT NULL, reason TEXT,
  needs_approval INTEGER DEFAULT 0, approved INTEGER DEFAULT 0, fill_price REAL, fill_ts TEXT);
CREATE TABLE IF NOT EXISTS order_events(id INTEGER PRIMARY KEY AUTOINCREMENT, order_id INTEGER, ts TEXT, status TEXT, note TEXT);
CREATE TABLE IF NOT EXISTS position_state(symbol TEXT PRIMARY KEY, market TEXT, stop REAL, initial_stop REAL,
  target REAL, highest_close REAL, atr REAL, entry_ts TEXT, decision_id INTEGER, memo_id INTEGER);
CREATE TABLE IF NOT EXISTS bars(symbol TEXT, date TEXT, open REAL, high REAL, low REAL, close REAL, volume REAL,
  final INTEGER, fetched_at TEXT, PRIMARY KEY(symbol,date));
CREATE TABLE IF NOT EXISTS fundamentals(symbol TEXT PRIMARY KEY, fetched_at TEXT, data TEXT);
CREATE TABLE IF NOT EXISTS equity_curve(date TEXT PRIMARY KEY, equity REAL, cash REAL, positions REAL,
  flows REAL, stale_symbols TEXT, benchmark REAL);
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, level TEXT, source TEXT, message TEXT);
CREATE TABLE IF NOT EXISTS memos(id INTEGER PRIMARY KEY AUTOINCREMENT, decision_id INTEGER, symbol TEXT, ts TEXT, text TEXT);
CREATE TABLE IF NOT EXISTS reports(id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, period TEXT, ts TEXT, hash TEXT, body TEXT,
  UNIQUE(kind,period));
CREATE TABLE IF NOT EXISTS backtests(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, label TEXT, result TEXT);
CREATE TABLE IF NOT EXISTS heartbeat(name TEXT PRIMARY KEY, ts TEXT, info TEXT);
CREATE TABLE IF NOT EXISTS source_status(name TEXT PRIMARY KEY, ts TEXT, ok INTEGER, detail TEXT);
"""

def conn() -> sqlite3.Connection:
    p = config.db_path()
    Path(p).parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(p, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA foreign_keys=ON")
    return c

def init():
    from . import migrations
    c = conn(); c.executescript(SCHEMA); c.commit(); c.close()
    migrations.apply_all(config.db_path())

def audit(action, detail, actor="system", source="engine", ts=None):
    from datetime import datetime, timezone
    c = conn()
    c.execute("INSERT INTO audit_log(ts,actor,action,detail,source) VALUES(?,?,?,?,?)",
              (ts or datetime.now(timezone.utc).isoformat(timespec="seconds"), actor, action, json.dumps(detail, default=str) if not isinstance(detail, str) else detail, source))
    c.commit(); c.close()

def get_settings() -> dict:
    c = conn()
    rows = c.execute("SELECT key,value FROM settings").fetchall(); c.close()
    s = json.loads(json.dumps(config.DEFAULTS))
    for r in rows: s[r["key"]] = json.loads(r["value"])
    return s

def set_settings(d: dict):
    c = conn()
    for k, v in d.items():
        if k not in config.DEFAULTS: raise KeyError(f"unknown setting {k}")
        c.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k, json.dumps(v)))
    c.commit(); c.close()

def event(level, source, message, ts=None):
    from datetime import datetime, timezone
    c = conn()
    c.execute("INSERT INTO events(ts,level,source,message) VALUES(?,?,?,?)",
              (ts or datetime.now(timezone.utc).isoformat(timespec="seconds"), level, source, message))
    c.commit(); c.close()

def heartbeat(name, info=""):
    from datetime import datetime, timezone
    c = conn()
    c.execute("INSERT INTO heartbeat(name,ts,info) VALUES(?,?,?) ON CONFLICT(name) DO UPDATE SET ts=excluded.ts,info=excluded.info",
              (name, datetime.now(timezone.utc).isoformat(timespec="seconds"), info))
    c.commit(); c.close()
