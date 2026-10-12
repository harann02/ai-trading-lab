"""Weekly evaluation report: P&L, win rate, avg win/loss, profit factor (with insufficient-sample handling), expectancy = WR*avgWin - LR*avgLoss, max drawdown, exposure, cash utilisation,
costs, benchmark comparison, breakdown by stock and by signal. Improvement recommendations are PROPOSALS ONLY: nothing here ever changes a live parameter."""
import json
from datetime import datetime, timezone
import numpy as np
from .. import db, portfolio, ledger, views
from . import shadow

MIN_TRADES_STATS = 10; MIN_TRADES_CONCLUSION = 30

def stats(trades):
    n = len(trades)
    if n == 0: return {"n_trades": 0, "note": "no closed trades yet: win rate / profit factor / expectancy are NOT defined"}
    pnl = np.array([t["pnl_usd"] for t in trades]); w = pnl[pnl > 0]; l = pnl[pnl < 0]
    wr = len(w) / n; lr = len(l) / n; aw = float(w.mean()) if len(w) else 0.0; al = float(-l.mean()) if len(l) else 0.0
    pf = (float(w.sum() / -l.sum()) if len(l) else None)
    out = {"n_trades": n, "wins": int(len(w)), "losses": int(len(l)), "win_rate": wr, "avg_win_usd": aw, "avg_loss_usd": al, "net_pnl_usd": float(pnl.sum()),
           "profit_factor": pf if n >= MIN_TRADES_STATS else None, "expectancy_usd": wr * aw - lr * al, "costs_usd": float(sum(t["fees_usd"] + t["cost_usd_spread_slip"] for t in trades))}
    notes = []
    if n < MIN_TRADES_STATS: notes.append(f"only {n} closed trade(s): profit factor withheld (needs >= {MIN_TRADES_STATS}); other ratios are anecdotal")
    elif not len(l): notes.append("no losing trades: profit factor undefined (not infinity)")
    if n < MIN_TRADES_CONCLUSION: notes.append(f"sample < {MIN_TRADES_CONCLUSION} trades: no conclusion about edge can be drawn")
    out["notes"] = notes; return out

def build(now=None, store=True):
    now = now or datetime.now(timezone.utc); cfg = db.get_settings(); snap = portfolio.snapshot(now, cfg); tr = snap["trades"]
    by_stock = {}; by_signal = {}
    c = db.conn()
    for t in tr:
        by_stock.setdefault(t["symbol"], []).append(t)
        sig = "unknown"
        if t.get("order_id"):
            r = c.execute("SELECT d.snapshot FROM orders o JOIN decisions d ON d.id=o.decision_id WHERE o.id=?", (t["order_id"],)).fetchone()
            # exit orders reference the exit decision; look for the entry order of the same symbol
            r2 = c.execute("SELECT d.snapshot FROM orders o JOIN decisions d ON d.id=o.decision_id WHERE o.symbol=? AND o.side='BUY' AND o.status='filled' AND o.strategy_id=? ORDER BY o.id DESC LIMIT 1", (t["symbol"], t["strategy_id"])).fetchone()
            try: sig = ((json.loads((r2 or r)["snapshot"]).get("extra") or {}).get("structure")) or "unknown"
            except Exception: sig = "unknown"
        by_signal.setdefault(sig, []).append(t)
    c.close()
    cmp = shadow.comparison(now, cfg); k = views.kpis(cfg, now)
    sl = snap["sleeves"]; eq = sum(s["equity_usd"] for s in sl.values()); pos = sum(s["positions_usd"] for s in sl.values())
    body = {"week_ending_myt": now.astimezone(ledger.MYT).strftime("%Y-%m-%d"), "generated_utc": now.isoformat(timespec="seconds"), "overall": stats(tr),
            "account": {"equity_myr": snap["equity_myr"], "total_pnl_myr": k["total_pnl_myr"], "total_return_pct": k.get("total_return_pct"), "weekly_return_pct": k.get("weekly_return_pct"), "max_drawdown_pct": k.get("max_drawdown_pct"),
                        "exposure_pct": (pos / eq * 100) if eq else 0.0, "cash_utilization_pct": (pos / eq * 100) if eq else 0.0, "costs_note": "FX spread + fees + modelled spread/slippage in USD/MYR on the sleeve"},
            "by_stock": {s: stats(v) for s, v in by_stock.items()}, "by_signal": {s: stats(v) for s, v in by_signal.items()},
            "benchmarks": [{kk: r.get(kk) for kk in ("key", "total_return_pct", "max_drawdown_pct", "value_myr", "monthly_return_pct")} for r in cmp["rows"]], "benchmark_note": cmp.get("note"),
            "proposals": proposals(stats(tr), cfg, cmp), "authority": "PROPOSALS ONLY. No live parameter is changed automatically; changes need owner authorization and the experiment process in docs/EXPERIMENT_PROCESS.md."}
    if store:
        from .. import research; research._store("weekly_eval", body["week_ending_myt"], body, now)
    return body

def proposals(st, cfg, cmp):
    out = []
    if st.get("n_trades", 0) < MIN_TRADES_CONCLUSION: out.append({"proposal": "Change nothing yet.", "why": f"{st.get('n_trades', 0)} closed trades < {MIN_TRADES_CONCLUSION}: any parameter change would be curve-fitting noise.", "status": "PROPOSED (no action)"})
    me = (cmp.get("rows") or [{}])[0]
    if me.get("exposure_pct_now") is not None and me["exposure_pct_now"] < 20: out.append({"proposal": "Review whether the weekly entry cadence leaves capital idle too often (see docs/ENTRY_CADENCE_STUDY.md).", "why": f"exposure is {me['exposure_pct_now']}% vs a fully invested benchmark; cash is a valid decision, but opportunity cost is real.", "status": "PROPOSED (needs owner authorization + experiment)"})
    if st.get("costs_usd") and st.get("net_pnl_usd") is not None and st["costs_usd"] > abs(st["net_pnl_usd"]): out.append({"proposal": "Costs exceed net P&L: examine trade frequency and position size vs costs.", "why": "cost drag dominates the result", "status": "PROPOSED"})
    return out
