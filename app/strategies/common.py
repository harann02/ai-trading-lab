"""Shared pieces for independent strategy engines. Each engine has its own sleeve (capital), orders/fills/P&L carry strategy_id."""
import json, math
from datetime import datetime, timezone
import numpy as np, pandas as pd
from .. import db, config, data, ledger, risk, scoring, calendars as cal, portfolio, engine

STATUSES = ["BUY ELIGIBLE", "WATCH", "HOLD", "EXIT", "REJECT", "DATA BLOCKED"]

def chk(checks, name, ok, detail): checks.append({"name": name, "pass": bool(ok), "detail": detail})

def strategy_cfg(cfg, sid): return cfg["strategies"].get(sid, {"enabled": False, "allocation_pct": 0.0})

def is_enabled(cfg, sid, caps_ok=True):
    st = strategy_cfg(cfg, sid); c = db.conn(); r = c.execute("SELECT suspended FROM strategy_state WHERE strategy_id=?", (sid,)).fetchone(); c.close()
    return bool(st["enabled"] and caps_ok and not (r and r["suspended"]))

def sleeve_context(snap, sid):
    return snap["sleeves"].get(sid)

def portfolio_gate(cfg, snap, sid, n_pos_total, entries_suspended):
    """Common portfolio checks, computed from sleeve + account numbers (USD)."""
    s = snap["sleeves"].get(sid) or {"equity_usd": 0, "cash_usd": 0, "buying_power_usd": 0, "positions_usd": 0}
    return s

def adv_usd(f): 
    v = f.get("adv20_local"); return None if v is None or pd.isna(v) else float(v)

def pending_orders(sid=None):
    c = db.conn(); q = "SELECT * FROM orders WHERE status='pending' AND strategy_id IS NOT NULL" + (" AND strategy_id=?" if sid else "")
    r = [dict(x) for x in c.execute(q, (sid,) if sid else ())]; c.close(); return r

def save_signal(key, now, sd, s, sid, d, mode="LIVE"):
    """Immutable decision row with strategy_id (decision_key unique -> repeated scans never duplicate)."""
    c = db.conn()
    try:
        cur = c.execute("INSERT INTO decisions(decision_key,ts,scan_date,symbol,market,algo_version,data_mode,decision,score,band,snapshot,scores,checks,plan,explanation,strategy_id)"
                        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (key, ledger.iso(now), str(sd), s, "US", d.get("algo", config.ALGO_VERSION), mode, d["decision"], d.get("score"), d.get("band"),
                         engine._j(d.get("snapshot")), engine._j(d.get("scores")), engine._j(d.get("checks")), engine._j(d.get("plan")), d.get("explanation", ""), sid))
        c.commit(); return cur.lastrowid
    except Exception as e:
        if "UNIQUE" in str(e): return None
        raise
    finally: c.close()

def size_for(cfg, snap, sid, plan_entry, plan_stop, f):
    s = snap["sleeves"].get(sid)
    if not s: return {"qty": 0, "binding": "no_allocation", "detail": {}}
    held_usd = s["positions_usd"]; total_eq_usd = snap["equity_usd"] or s["equity_usd"]
    reserve = s["equity_usd"] * cfg["min_cash_pct"] / 100
    usable = max(0.0, s["buying_power_usd"] - reserve)
    return risk.size_position_usd(equity_usd=total_eq_usd, usable_cash_usd=usable, price=plan_entry, stop=plan_stop, cfg=cfg, adv_usd=adv_usd(f),
                                  sleeve_equity_usd=s["equity_usd"], sleeve_exposure_usd=held_usd)

def common_checks(checks, cfg, snap, sid, symbol, qty, entry, n_pos_total, held_syms, pend_syms, suspended):
    s = snap["sleeves"].get(sid, {"equity_usd": 0, "cash_usd": 0, "buying_power_usd": 0, "positions_usd": 0})
    cost = qty * entry
    chk(checks, "strategy_enabled", cfg["strategies"][sid]["enabled"], f"{sid} enabled={cfg['strategies'][sid]['enabled']}")
    chk(checks, "entries_not_suspended", not suspended, "drawdown suspension active (manual resume required)" if suspended else "ok")
    chk(checks, "max_positions", n_pos_total < cfg["max_positions"], f"{n_pos_total}/{cfg['max_positions']} account positions")
    chk(checks, "min_cash_reserve", s["cash_usd"] - cost >= s["equity_usd"] * cfg["min_cash_pct"] / 100 - 1e-6, f"sleeve cash after ${s['cash_usd']-cost:,.2f} vs reserve ${s['equity_usd']*cfg['min_cash_pct']/100:,.2f}")
    chk(checks, "max_exposure", s["positions_usd"] + cost <= s["equity_usd"] * cfg["max_exposure_pct"] / 100 + 1e-6, f"sleeve exposure after ${s['positions_usd']+cost:,.2f} vs max ${s['equity_usd']*cfg['max_exposure_pct']/100:,.2f}")
    chk(checks, "settled_cash", s["buying_power_usd"] >= cost - 1e-6, f"buying power ${s['buying_power_usd']:,.2f} (settled cash) vs cost ${cost:,.2f}")
    chk(checks, "not_blocked_manual", symbol not in cfg["blocked_symbols"], "manual block list")
    chk(checks, "not_held_or_pending", symbol not in held_syms and symbol not in pend_syms, "no duplicate position/order in this strategy")
    chk(checks, "automation_on", cfg.get("automation_on", True) and cfg["auto_trade"], "automation ON" if cfg.get("automation_on", True) and cfg["auto_trade"] else "automation OFF: decision only")

def summary_metrics(sid, snap, trades, curve_rows, rf=0.03):
    """Per-strategy metrics: win rate, max DD, holding period, Sharpe, Sortino, costs, return on allocated capital."""
    t = [x for x in trades if x["strategy_id"] == sid]; s = snap["sleeves"].get(sid)
    out = {"strategy_id": sid, "n_trades": len(t), "allocated_myr": s["invested_myr"] if s else 0.0}
    if t:
        p = np.array([x["pnl_usd"] for x in t]); out["win_rate"] = float((p > 0).mean()); out["avg_pnl_usd"] = float(p.mean())
        out["avg_holding_days"] = float(np.mean([(pd.Timestamp(x["exit_ts"]) - pd.Timestamp(x["entry_ts"])).total_seconds() / 86400 for x in t]))
        out["realized_usd"] = float(p.sum())
    else: out.update({"win_rate": None, "avg_holding_days": None, "realized_usd": 0.0})
    if s:
        out.update({"fees_usd": s["fees_usd"], "spread_slippage_usd": s["spread_usd"] + s["slip_usd"], "fx_spread_myr": s["fx_spread_paid_myr"],
                    "return_on_allocated_pct": s["return_on_allocated_pct"], "equity_myr": s["equity_myr"], "total_pnl_myr": s["total_pnl_myr"],
                    "stock_pnl_usd": s["stock_pnl_usd"], "currency_pnl_myr": s["currency_pnl_myr"]})
    eq = pd.Series({r["date"]: r["equity_usd"] for r in curve_rows if r["strategy_id"] == sid}).sort_index() if curve_rows else pd.Series(dtype=float)
    if len(eq) >= 2:
        out["max_drawdown_pct"] = float((eq / eq.cummax() - 1).min() * 100); r = eq.pct_change().dropna(); out["n_daily_returns"] = len(r)
        if len(r) >= 20 and r.std() > 0:
            ex = r.mean() - rf / 252; dd = np.sqrt((np.minimum(r - rf / 252, 0) ** 2).mean())
            out["sharpe"] = float(ex / r.std() * math.sqrt(252)); out["sortino"] = float(ex / dd * math.sqrt(252)) if dd > 0 else None
        else: out["sharpe"] = out["sortino"] = None; out["note"] = "fewer than 20 daily returns: Sharpe/Sortino not computed (statistically meaningless)"
    else: out.update({"max_drawdown_pct": None, "sharpe": None, "sortino": None, "note": "insufficient equity history"})
    return out

def after_hold(ctx, sid, s, d, c, trailing=False):
    """For a held position: ratchet trailing stop (swing) and, if an exit rule fired, create the SELL order (fills at next open). Idempotent per (strategy,symbol,last bar)."""
    cfg = ctx.cfg; f = c["f"]; ps = ctx.pos_state(sid, s)
    if ps and trailing and not pd.isna(f["atr14"]):
        df = ctx.bars(s); post = df[df.index >= pd.Timestamp(ps["entry_ts"][:10])]
        hi = max(ps["highest_close"] or 0, float(post["close"].max())) if len(post) else ps["highest_close"]
        new_stop = max(ps["stop"], hi - cfg["atr_stop_mult"] * float(f["atr14"]))
        cn = db.conn(); cn.execute("UPDATE position_state2 SET highest_close=?, stop=?, atr=? WHERE strategy_id=? AND symbol=?", (hi, new_stop, float(f["atr14"]), sid, s)); cn.commit(); cn.close()
    if d.get("exit_reason"):
        pos = portfolio.replay2([r for r in ledger.rows() if r.get("strategy_id")])["positions"].get((sid, s))
        if pos:
            oid = engine.create_order(decision_key=f"EXIT:{sid}:{s}:{c['st'].get('last_bar')}", decision_id=d.get("id"), symbol=s, market="US", side="SELL", qty=pos["qty"], ref_price=float(f["close"]),
                                      stop=None, target=None, reason=d["exit_reason"], now=ctx.now, strategy_id=sid, signal_status="EXIT")
            if oid: db.event("INFO", "monitor", f"[{sid}] EXIT order #{oid} pending for {s}: {d['exit_reason']}", ledger.iso(ctx.now))
