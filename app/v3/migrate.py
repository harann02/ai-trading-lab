"""v3.0 schema + data migration (migration #3). Runs inside migrations.apply_all (one transaction; a verified copy of the DB is made first by archive_before).
DATA STEPS (only when the DB already holds v2 ledger rows):
  1. snapshot the v2 account into v2_archive (sleeves, orders, FX conversions, curve, settings) -> the archived v2 account view
  2. cancel PENDING BUY orders of retired sleeves (baseline, meanrev): they are unfilled, so cancelling is not a sale; every cancel has order_events + audit rows
  3. move the retired sleeves' free USD cash into the swing sleeve with paired SLEEVE_XFER_OUT / SLEEVE_XFER_IN ledger rows: cash_delta_myr = 0, no FX conversion, no
     fee, not a DEPOSIT, no realized P&L; the MYR cost basis and the FX spread already paid travel with the cash (replay2 understands the kinds)
  4. NEVER sells: open positions in retired sleeves are left in place and a PENDING_OWNER_APPROVAL proposal is written (migration_proposals)
  5. v3 settings (swing 100%, mean reversion/baseline 0%) replace v2 allocations; v2 values are preserved in settings_archive"""
import json
from datetime import datetime, timezone

ORDER_COLS = [("signal_ts", "TEXT"), ("data_ts", "TEXT"), ("order_type", "TEXT"), ("eligible_session", "TEXT"), ("eligible_ts", "TEXT"), ("data_source", "TEXT"),
              ("price_available_ts", "TEXT"), ("exec_ts", "TEXT"), ("exec_price", "REAL"), ("spread_bps", "REAL"), ("slippage_usd", "REAL"), ("fees_usd", "REAL"),
              ("fx_rate", "REAL"), ("fill_model_version", "TEXT"), ("fill_kind", "TEXT"), ("fill_basis", "TEXT"), ("observed_ts", "TEXT")]

SCHEMA = """
CREATE TABLE IF NOT EXISTS strategy_flags(strategy_id TEXT PRIMARY KEY, new_entries_enabled INTEGER NOT NULL, label TEXT, explanation TEXT, updated_ts TEXT);
CREATE TABLE IF NOT EXISTS v3_migration_log(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, step TEXT, detail TEXT);
CREATE TRIGGER IF NOT EXISTS v3log_no_update BEFORE UPDATE ON v3_migration_log BEGIN SELECT RAISE(ABORT,'v3_migration_log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS v3log_no_delete BEFORE DELETE ON v3_migration_log BEGIN SELECT RAISE(ABORT,'v3_migration_log is append-only'); END;
CREATE TABLE IF NOT EXISTS v2_archive(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, kind TEXT, payload TEXT);
CREATE TRIGGER IF NOT EXISTS v2arch_no_update BEFORE UPDATE ON v2_archive BEGIN SELECT RAISE(ABORT,'v2_archive is append-only'); END;
CREATE TRIGGER IF NOT EXISTS v2arch_no_delete BEFORE DELETE ON v2_archive BEGIN SELECT RAISE(ABORT,'v2_archive is append-only'); END;
CREATE TABLE IF NOT EXISTS migration_proposals(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, kind TEXT, strategy_id TEXT, symbol TEXT, detail TEXT, status TEXT, decided_ts TEXT, decided_by TEXT);
CREATE TABLE IF NOT EXISTS job_runs(id INTEGER PRIMARY KEY AUTOINCREMENT, idem_key TEXT UNIQUE, job TEXT, trigger TEXT, scheduled_for TEXT, started_ts TEXT, finished_ts TEXT,
  status TEXT, skip_reason TEXT, error TEXT, run_id TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS ledger_meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS writer_lease(name TEXT PRIMARY KEY, holder TEXT, mode TEXT, acquired_ts TEXT, expires_ts TEXT, run_id TEXT);
CREATE TABLE IF NOT EXISTS forward_sessions(session_date TEXT PRIMARY KEY, scan_occurred INTEGER, data_retrieved INTEGER, stocks_evaluated INTEGER, candidates INTEGER, passed_risk INTEGER,
  proposed INTEGER, submitted INTEGER, rejected INTEGER, filled INTEGER, positions_reconciled INTEGER, pnl_correct INTEGER, daily_report INTEGER, errors INTEGER, stale_feeds INTEGER,
  missed_runs INTEGER, status TEXT, qualifies INTEGER, notes TEXT, updated_ts TEXT);
CREATE TABLE IF NOT EXISTS data_requests(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, provider TEXT, kind TEXT, n_symbols INTEGER, retrieved_ts TEXT, exchange_ts TEXT,
  completeness REAL, symbols_valid INTEGER, symbols_invalid TEXT, staleness TEXT, ok INTEGER, label TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS shadow_portfolios(id TEXT PRIMARY KEY, symbol TEXT, start_date TEXT, start_myr REAL, start_fx REAL, shares REAL, cash_usd REAL, entry_price REAL,
  costs_usd REAL, fx_cost_myr REAL, method TEXT, created_ts TEXT);
CREATE TABLE IF NOT EXISTS shadow_values(pid TEXT, date TEXT, price_usd REAL, price_basis TEXT, shares REAL, cash_usd REAL, value_usd REAL, fx REAL, value_myr REAL, PRIMARY KEY(pid,date));
CREATE TABLE IF NOT EXISTS ai_status_log(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, state TEXT, provider TEXT, model TEXT, category TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS account_epochs(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, label TEXT, archive_path TEXT, note TEXT);
"""

def _cols(c, t): return {r[1] for r in c.execute(f"PRAGMA table_info({t})")}
def _log(c, ts, step, detail): c.execute("INSERT INTO v3_migration_log(ts,step,detail) VALUES(?,?,?)", (ts, step, json.dumps(detail, default=str)))

def v3_settings():
    return {"strategies": {"swing": {"enabled": True, "allocation_pct": 100.0}, "meanrev": {"enabled": True, "allocation_pct": 0.0},
                           "baseline": {"enabled": False, "allocation_pct": 0.0}, "intraday": {"enabled": False, "allocation_pct": 0.0}}}

def totals(c):
    """Pure-SQL account totals used for before/after reconciliation inside the migration (valuation-independent)."""
    q = lambda s: c.execute(s).fetchone()[0] or 0.0
    sl = {r[0]: {"cash_usd": r[1], "usd_received": r[2], "invested_myr": r[3]} for r in c.execute(
        "SELECT strategy_id, SUM(cash_delta_usd), SUM(CASE WHEN kind='FX_CONVERT' THEN gross_usd ELSE 0 END), SUM(CASE WHEN kind='FX_CONVERT' THEN gross_myr ELSE 0 END) FROM ledger WHERE strategy_id IS NOT NULL GROUP BY strategy_id")}
    return {"ledger_rows": c.execute("SELECT COUNT(*) FROM ledger").fetchone()[0], "cash_myr": q("SELECT SUM(cash_delta_myr) FROM ledger"),
            "cash_usd_total": q("SELECT SUM(cash_delta_usd) FROM ledger"), "deposits_myr": q("SELECT SUM(cash_delta_myr) FROM ledger WHERE kind IN ('DEPOSIT','WITHDRAW')"),
            "fx_spread_paid_myr": q("SELECT SUM(fees_myr) FROM ledger WHERE kind='FX_CONVERT'"), "realized_pnl_myr": q("SELECT SUM(realized_pnl_myr) FROM ledger"),
            "fx_converted_myr": q("SELECT SUM(gross_myr) FROM ledger WHERE kind='FX_CONVERT'"), "fx_received_usd": q("SELECT SUM(gross_usd) FROM ledger WHERE kind='FX_CONVERT'"), "sleeves": sl}

def create_schema(c):
    """DDL (idempotent). Runs BEFORE the migration transaction because executescript() commits implicitly."""
    c.executescript(SCHEMA)

def m3_v3_core(c):
    from .. import portfolio
    from . import policy
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    have = _cols(c, "orders")
    for col, decl in ORDER_COLS:
        if col not in have: c.execute(f"ALTER TABLE orders ADD COLUMN {col} {decl}")
    policy.ensure_flags(c)
    c.execute("INSERT OR IGNORE INTO ledger_meta VALUES('writer_mode','actions')")
    c.execute("INSERT OR IGNORE INTO ledger_meta VALUES('v3_applied_ts',?)", (now,))
    rows = [dict(zip([d[0] for d in c.execute("SELECT * FROM ledger LIMIT 0").description], r)) for r in c.execute("SELECT * FROM ledger ORDER BY id")]
    v2rows = [r for r in rows if r.get("strategy_id")]
    pre = totals(c)
    _log(c, now, "start", {"pre_totals": pre})
    # v2 settings are preserved (settings_archive) before the v3 policy replaces allocations
    for r in c.execute("SELECT key,value FROM settings WHERE key='strategies'").fetchall():
        c.execute("INSERT INTO settings_archive(ts,version,key,value) VALUES(?,?,?,?)", (now, "v2", r[0], r[1]))
    if not v2rows:
        c.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('strategies',?)", (json.dumps(v3_settings()["strategies"]),))
        _log(c, now, "fresh_or_empty_ledger", {"note": "no v2 ledger rows: schema + v3 settings only"}); return {"data_migration": False}
    # 1. archive snapshot of the v2 account
    rp = portfolio.replay2(v2rows); cut = max(r["id"] for r in rows)
    snap = {"as_of_ts": now, "ledger_cut_id": cut, "sleeves": {k: {kk: vv for kk, vv in v.items() if kk != "unsettled_rows"} for k, v in rp["sleeves"].items()},
            "positions": [{"strategy_id": k[0], "symbol": k[1], **{a: b for a, b in p.items()}} for k, p in rp["positions"].items()], "totals": pre}
    snap["orders"] = [dict(zip([d[0] for d in c.execute("SELECT * FROM orders LIMIT 0").description], r)) for r in c.execute("SELECT * FROM orders ORDER BY id")]
    snap["fx_conversions"] = [dict(zip([d[0] for d in c.execute("SELECT * FROM fx_conversions LIMIT 0").description], r)) for r in c.execute("SELECT * FROM fx_conversions ORDER BY id")]
    snap["equity_curve2"] = [dict(zip([d[0] for d in c.execute("SELECT * FROM equity_curve2 LIMIT 0").description], r)) for r in c.execute("SELECT * FROM equity_curve2 ORDER BY date,strategy_id")]
    snap["settings_v2"] = {r[0]: json.loads(r[1]) for r in c.execute("SELECT key,value FROM settings")}
    c.execute("INSERT INTO v2_archive(ts,kind,payload) VALUES(?,?,?)", (now, "v2_account_at_migration", json.dumps(snap, default=str)))
    # 2. cancel pending BUY orders of retired sleeves (unfilled -> not a sale)
    cancelled = []
    for o in c.execute("SELECT id,strategy_id,symbol,qty FROM orders WHERE status='pending' AND side='BUY' AND strategy_id IN ('baseline','meanrev')").fetchall():
        why = "v3.0 migration: sleeve retired (baseline) / research-only (meanrev); order was unfilled so cancelling is not a sale"
        c.execute("UPDATE orders SET status='canceled', reason=?, updated_ts=? WHERE id=?", (why, now, o[0]))
        c.execute("INSERT INTO order_events(order_id,ts,status,note) VALUES(?,?,?,?)", (o[0], now, "canceled", why))
        c.execute("INSERT INTO audit_log(ts,actor,action,detail,source) VALUES(?,?,?,?,?)", (now, "v3-migration", "order_canceled", json.dumps({"order_id": o[0], "strategy": o[1], "symbol": o[2], "qty": o[3], "reason": why}), "migration"))
        cancelled.append({"id": o[0], "strategy": o[1], "symbol": o[2], "qty": o[3]})
    _log(c, now, "orders_canceled", cancelled)
    # 3/4. sleeve cash -> swing; never sell
    moved = []; proposals = []
    held = {}
    for (sid, sym), p in rp["positions"].items(): held.setdefault(sid, []).append((sym, p))
    for sid in ("baseline", "meanrev"):
        s = rp["sleeves"].get(sid)
        if not s: continue
        for sym, p in held.get(sid, []):
            detail = {"strategy": sid, "symbol": sym, "qty": p["qty"], "cost_usd": p["cost_usd"], "options": ["keep until protective exit / stop (default, no action needed)", "owner-approved SELL at next eligible session (create a sell order)"],
                      "reason": "sleeve retired in v3.0; position was NOT sold automatically"}
            c.execute("INSERT INTO migration_proposals(ts,kind,strategy_id,symbol,detail,status) VALUES(?,?,?,?,?,?)", (now, "HOLDING_IN_RETIRED_SLEEVE", sid, sym, json.dumps(detail), "PENDING_OWNER_APPROVAL"))
            proposals.append(detail)
        cash = s["cash_usd"]
        if cash <= 1e-9: continue
        frac = cash / s["usd_received"] if s["usd_received"] else 0.0
        myr_b = s["invested_myr"] * frac; spr = s["fx_spread_myr"] * frac; basis = (s["invested_myr"] / s["usd_received"]) if s["usd_received"] else None
        base = dict(ccy="USD", market=None, fx=basis, fx_basis=basis)
        for kind, tsid, sgn in (("SLEEVE_XFER_OUT", sid, -1), ("SLEEVE_XFER_IN", "swing", 1)):
            c.execute("INSERT INTO ledger(ts,kind,symbol,market,qty,price_local,ccy,fx,gross_myr,fees_myr,slippage_myr,cash_delta_myr,uniq,note,strategy_id,cash_delta_usd,gross_usd,fx_basis,fee_detail) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (now, kind, None, None, None, None, "USD", basis, myr_b, 0.0, 0.0, 0.0, f"V3XFER:{sid}:{kind}", f"v3.0 internal sleeve transfer {sid} -> swing: USD{cash:.2f} (cost basis RM{myr_b:.2f}); journal entry, not a deposit, no FX, no fee, no P&L",
                       tsid, sgn * cash, cash, basis, json.dumps({"fx_spread_myr_carried": spr, "from": sid, "to": "swing"})))
        moved.append({"from": sid, "to": "swing", "usd": cash, "myr_basis": myr_b, "fx_spread_myr_carried": spr}); 
    _log(c, now, "sleeve_transfers", moved); _log(c, now, "proposals", proposals)
    # 5. settings
    c.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('strategies',?)", (json.dumps(v3_settings()["strategies"]),))
    post = totals(c)
    ok = abs(post["cash_myr"] - pre["cash_myr"]) < 1e-6 and abs(post["cash_usd_total"] - pre["cash_usd_total"]) < 1e-6 and abs(post["deposits_myr"] - pre["deposits_myr"]) < 1e-9 and abs(post["fx_converted_myr"] - pre["fx_converted_myr"]) < 1e-9 and abs(post["realized_pnl_myr"] - pre["realized_pnl_myr"]) < 1e-9
    if not ok: raise RuntimeError(f"v3 migration reconciliation failed: pre={pre} post={post}")
    _log(c, now, "complete", {"post_totals": post, "reconciled": True, "ledger_rows_added": post["ledger_rows"] - pre["ledger_rows"]})
    c.execute("INSERT INTO events(ts,level,source,message) VALUES(?,?,?,?)", (now, "INFO", "migration", "v3.0 migration applied: swing 100% of pool, mean reversion research-only, baseline retired, orders canceled with audit, sleeve cash moved by journal entries"))
    c.execute("INSERT INTO audit_log(ts,actor,action,detail,source) VALUES(?,?,?,?,?)", (now, "v3-migration", "migration_v3_applied", json.dumps({"moved": moved, "canceled": cancelled, "proposals": len(proposals)}), "migration"))
    return {"data_migration": True, "moved": moved, "canceled": cancelled, "proposals": proposals, "pre": pre, "post": post}
