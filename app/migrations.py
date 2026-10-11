"""Versioned, forward-only schema migrations. v1 baseline (CREATE IF NOT EXISTS in db.SCHEMA) is migration 1.
Before ANY pending migration touches a database that already holds ledger history, the file is copied (SQLite online-backup API)
to <db dir>/archive/<name>-premigrate-v<N>-<UTC stamp>.db. Nothing is ever deleted: v1 Bursa rows are tagged as archived (read-only), v1 settings are
copied to settings_archive before being replaced."""
import json, sqlite3
from datetime import datetime, timezone
from pathlib import Path

def _cols(c, t): return {r[1] for r in c.execute(f"PRAGMA table_info({t})")}
def _add(c, t, col, decl):
    if col not in _cols(c, t): c.execute(f"ALTER TABLE {t} ADD COLUMN {col} {decl}")

def m1_baseline(c): pass   # tables created by db.SCHEMA (idempotent)

def m2_v2_core(c):
    """v2.0: strategies, USD cash, FX audit, settlement, universe, providers, control plane, AI log, archives."""
    for col, decl in [("strategy_id", "TEXT"), ("cash_delta_usd", "REAL DEFAULT 0"), ("gross_usd", "REAL"), ("fees_usd", "REAL"), ("spread_usd", "REAL"),
                      ("slip_usd", "REAL"), ("fx_basis", "REAL"), ("settle_date", "TEXT"), ("realized_stock_usd", "REAL"), ("fx_conv_id", "INTEGER"), ("fee_detail", "TEXT")]:
        _add(c, "ledger", col, decl)
    _add(c, "orders", "strategy_id", "TEXT"); _add(c, "decisions", "strategy_id", "TEXT")
    _add(c, "orders", "signal_status", "TEXT"); _add(c, "orders", "plan_json", "TEXT")
    c.executescript("""
    CREATE TABLE IF NOT EXISTS fx_conversions(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, direction TEXT NOT NULL, strategy_id TEXT,
      myr_amount REAL NOT NULL, usd_amount REAL NOT NULL, mid_rate REAL NOT NULL, spread_bps REAL NOT NULL, effective_rate REAL NOT NULL,
      spread_cost_myr REAL NOT NULL, provider TEXT, rate_ts TEXT, rate_source TEXT, note TEXT, ledger_uniq TEXT UNIQUE);
    CREATE TRIGGER IF NOT EXISTS fx_no_update BEFORE UPDATE ON fx_conversions BEGIN SELECT RAISE(ABORT,'fx audit is append-only'); END;
    CREATE TRIGGER IF NOT EXISTS fx_no_delete BEFORE DELETE ON fx_conversions BEGIN SELECT RAISE(ABORT,'fx audit is append-only'); END;
    CREATE TABLE IF NOT EXISTS fx_rates(date TEXT, provider TEXT, rate REAL, fetched_at TEXT, PRIMARY KEY(date,provider));
    CREATE TABLE IF NOT EXISTS position_state2(strategy_id TEXT, symbol TEXT, stop REAL, initial_stop REAL, target REAL, highest_close REAL, atr REAL,
      entry_ts TEXT, decision_id INTEGER, reason TEXT, PRIMARY KEY(strategy_id, symbol));
    CREATE TABLE IF NOT EXISTS strategy_state(strategy_id TEXT PRIMARY KEY, enabled INTEGER, suspended INTEGER DEFAULT 0, reason TEXT, updated_ts TEXT);
    CREATE TABLE IF NOT EXISTS equity_curve2(date TEXT, strategy_id TEXT, equity_myr REAL, equity_usd REAL, cash_usd REAL, positions_usd REAL, fx REAL,
      PRIMARY KEY(date, strategy_id));
    CREATE TABLE IF NOT EXISTS universe_cache(key TEXT PRIMARY KEY, fetched_at TEXT, payload TEXT);
    CREATE TABLE IF NOT EXISTS universe_members(symbol TEXT PRIMARY KEY, name TEXT, exchange TEXT, sources TEXT, passed INTEGER, reasons TEXT, metrics TEXT, asof TEXT);
    CREATE TABLE IF NOT EXISTS provider_caps(provider TEXT PRIMARY KEY, ts TEXT, active INTEGER, caps TEXT, note TEXT);
    CREATE TABLE IF NOT EXISTS audit_log(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, actor TEXT, action TEXT, detail TEXT, source TEXT);
    CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit_log BEGIN SELECT RAISE(ABORT,'audit_log is append-only'); END;
    CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit_log BEGIN SELECT RAISE(ABORT,'audit_log is append-only'); END;
    CREATE TABLE IF NOT EXISTS control_applied(command_id TEXT PRIMARY KEY, ts TEXT, status TEXT, detail TEXT);
    CREATE TABLE IF NOT EXISTS llm_log(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, kind TEXT, symbol TEXT, provider TEXT, model TEXT, mode TEXT,
      prompt TEXT, source_data TEXT, response TEXT, ok INTEGER, error TEXT);
    CREATE TABLE IF NOT EXISTS memos2(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, symbol TEXT, strategy_id TEXT, decision_id INTEGER, mode TEXT, fields TEXT, text TEXT, llm_log_id INTEGER);
    CREATE TABLE IF NOT EXISTS settings_archive(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, version TEXT, key TEXT, value TEXT);
    CREATE TABLE IF NOT EXISTS archived_records(table_name TEXT, row_id INTEGER, reason TEXT, archived_at TEXT, PRIMARY KEY(table_name,row_id));
    CREATE TABLE IF NOT EXISTS experiments(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, kind TEXT, label TEXT, result TEXT);
    CREATE TABLE IF NOT EXISTS scan_results(scan_date TEXT, strategy_id TEXT, ts TEXT, payload TEXT, PRIMARY KEY(scan_date,strategy_id));
    CREATE VIEW IF NOT EXISTS bursa_archive_decisions AS SELECT * FROM decisions WHERE market='BURSA';
    """)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    # archive v1 settings (never overwritten silently) and retire Bursa: tag rows read-only
    for r in c.execute("SELECT key,value FROM settings").fetchall():
        c.execute("INSERT INTO settings_archive(ts,version,key,value) VALUES(?,?,?,?)", (now, "v1", r[0], r[1]))
    for k in ("markets_enabled", "watchlist", "equity_benchmark", "min_trade_value_myr", "fx_spread_bps"):
        c.execute("DELETE FROM settings WHERE key=?", (k,))     # defaults (US-only) now apply; old values preserved in settings_archive
    c.execute("INSERT OR IGNORE INTO archived_records SELECT 'decisions', id, 'Bursa retired in v2.0 (read-only archive)', ?  FROM decisions WHERE market='BURSA'", (now,))
    c.execute("INSERT OR IGNORE INTO archived_records SELECT 'orders', id, 'Bursa retired in v2.0 (read-only archive)', ? FROM orders WHERE market='BURSA'", (now,))
    c.execute("INSERT OR IGNORE INTO archived_records SELECT 'ledger', id, 'Bursa retired in v2.0 (read-only archive)', ? FROM ledger WHERE market='BURSA'", (now,))
    # v1 pending orders were sized by the MYR/v1.0 model: superseded (audit trail kept in order_events)
    for o in c.execute("SELECT id FROM orders WHERE status='pending'").fetchall():
        c.execute("UPDATE orders SET status='canceled', reason='superseded by v2.0 migration (v1 order sized under MYR/v1 model; decision kept)', updated_ts=? WHERE id=?", (now, o[0]))
        c.execute("INSERT INTO order_events(order_id,ts,status,note) VALUES(?,?,?,?)", (o[0], now, "canceled", "superseded by v2.0 migration"))
    c.execute("INSERT INTO events(ts,level,source,message) VALUES(?,?,?,?)", (now, "INFO", "migration", "v2.0 migration applied: Bursa archived read-only, US-only universe, USD/FX/strategy tables created"))

MIGRATIONS = [(1, "v1_baseline", m1_baseline), (2, "v2_core", m2_v2_core)]
LATEST = MIGRATIONS[-1][0]

def current_version(c) -> int:
    c.execute("CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY, name TEXT, applied_at TEXT)")
    r = c.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
    return r[0] or 0

def archive_before(path: str, version: int) -> str | None:
    p = Path(path)
    if not p.exists(): return None
    src = sqlite3.connect(path)
    try:
        has = src.execute("SELECT name FROM sqlite_master WHERE name='ledger'").fetchone()
        n = src.execute("SELECT COUNT(*) FROM ledger").fetchone()[0] if has else 0
        if n == 0: return None
        arch = p.parent / "archive"; arch.mkdir(exist_ok=True)
        dest = arch / f"{p.stem}-premigrate-v{version}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.db"
        dst = sqlite3.connect(str(dest)); src.backup(dst); dst.close()
        return str(dest)
    finally: src.close()

def apply_all(path: str) -> list:
    """Applies pending migrations in order inside one transaction each. Returns list of applied (version, archive_path)."""
    c = sqlite3.connect(path, timeout=30); c.row_factory = sqlite3.Row; applied = []
    try:
        cur = current_version(c)
        has_ledger = c.execute("SELECT name FROM sqlite_master WHERE name='ledger'").fetchone()
        if cur == 0 and has_ledger and c.execute("SELECT COUNT(*) FROM ledger").fetchone()[0] > 0:
            c.execute("INSERT OR IGNORE INTO schema_migrations VALUES(1,'v1_baseline',?)", (datetime.now(timezone.utc).isoformat(timespec="seconds"),)); c.commit(); cur = 1
        for v, name, fn in MIGRATIONS:
            if v <= cur: continue
            arch = archive_before(path, v) if v > 1 else None
            try:
                c.execute("BEGIN"); fn(c)
                c.execute("INSERT INTO schema_migrations VALUES(?,?,?)", (v, name, datetime.now(timezone.utc).isoformat(timespec="seconds"))); c.commit()
            except Exception: c.rollback(); raise
            applied.append((v, arch))
    finally: c.close()
    return applied
