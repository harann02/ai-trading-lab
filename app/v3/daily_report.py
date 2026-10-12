"""Daily AI report generated at the end of each session (stored in reports kind='daily_ai', viewable on the dashboard). All numbers come from the ledger/DB;
the AI commentary is explanation only and is labeled by AI mode (LLM verified / template)."""
import json
from datetime import datetime, timezone, timedelta, date
from .. import db, ledger, portfolio, ai, views, calendars as cal
from . import shadow, schedule as S, verification as V

def build(session, now=None, store=True):
    now = now or datetime.now(timezone.utc); cfg = db.get_settings(); snap = portfolio.snapshot(now, cfg); k = views.kpis(cfg, now); c = db.conn()
    h = cal.session_hours("US", session); o, cl = h
    ds, de = o.isoformat(timespec="seconds"), (cl + timedelta(hours=8)).isoformat(timespec="seconds")
    fills = [dict(r) for r in c.execute("SELECT id,symbol,side,qty,status,reason,fill_price,fill_ts,exec_ts,price_available_ts,fill_basis,fees_usd,slippage_usd,spread_bps,strategy_id FROM orders WHERE strategy_id IS NOT NULL AND ((exec_ts>=? AND exec_ts<?) OR (updated_ts>=? AND updated_ts<? AND status IN ('rejected','canceled')))", (ds, de, ds, de))]
    pend = [dict(r) for r in c.execute("SELECT id,symbol,side,qty,ref_price,stop,target,status,reason,eligible_session FROM orders WHERE status='pending' AND strategy_id IS NOT NULL")]
    dec = [dict(r) for r in c.execute("SELECT symbol,strategy_id,decision,score,checks,plan FROM decisions WHERE scan_date=? AND strategy_id='swing' ORDER BY score DESC LIMIT 6", (str(session),))]
    top = []
    for d in dec:
        ch = json.loads(d["checks"] or "[]"); pl = json.loads(d["plan"] or "null") or {}
        top.append({"symbol": d["symbol"], "decision": d["decision"], "score": d["score"], "entry": pl.get("entry"), "stop": pl.get("stop"), "target": pl.get("target"), "rr": pl.get("rr"), "qty": pl.get("qty"),
                    "rejected_because": [x["name"] + ": " + x["detail"][:90] for x in ch if not x["pass"] and x["name"] != "automation_on"][:4]})
    ev_err = [dict(r) for r in c.execute("SELECT ts,source,message FROM events WHERE level IN ('ERROR','CRITICAL') AND ts>=? ORDER BY id DESC LIMIT 5", (ds,))]
    jobs = [dict(r) for r in c.execute("SELECT job,status,skip_reason,error FROM job_runs WHERE started_ts>=? AND started_ts<?", (ds, de))]; c.close()
    n_ok = len([j for j in jobs if j["status"] == "success"]); n_fail = len([j for j in jobs if j["status"] == "failed"]); n_skip = len([j for j in jobs if j["status"] == "skipped"])
    sleeves = snap["sleeves"]; eq_usd = sum(s["equity_usd"] for s in sleeves.values()); pos_usd = sum(s["positions_usd"] for s in sleeves.values()); cash_usd = sum(s["cash_usd"] for s in sleeves.values())
    reg = (views.discovery().get("regime") or {})
    cmp = shadow.comparison(now, cfg); bm = [{"key": r["key"], "total_return_pct": r.get("total_return_pct"), "value_myr": r.get("value_myr")} for r in cmp["rows"] if "key" in r]
    ver = V.summary(now); aist = ai.status()
    facts = {"session": str(session), "generated_utc": now.isoformat(timespec="seconds"), "generated_myt": now.astimezone(ledger.MYT).strftime("%Y-%m-%d %H:%M MYT"),
             "portfolio": {"equity_myr": round(snap["equity_myr"], 2), "equity_usd": round(snap["equity_usd"], 2) if snap["equity_usd"] else None, "usdmyr": snap["fx"].get("rate"), "cash_usd": round(cash_usd, 2),
                           "settled_cash_usd": round(sum(s["settled_cash_usd"] for s in sleeves.values()), 2), "unsettled_cash_usd": round(sum(s["unsettled_cash_usd"] for s in sleeves.values()), 2), "cash_myr": round(snap["cash_myr"], 2),
                           "invested_usd": round(pos_usd, 2), "exposure_pct": round(pos_usd / eq_usd * 100, 1) if eq_usd else 0.0, "today_pnl_myr": k.get("today_pnl_myr"), "total_pnl_myr": round(k["total_pnl_myr"], 2), "total_return_pct": round(k["total_return_pct"], 3) if k.get("total_return_pct") is not None else None},
             "positions": [{"symbol": x["symbol"], "qty": x["qty"], "value_usd": round(x["value_usd"], 2), "unrealized_usd": round(x["unrealized_usd"], 2), "last_bar": x.get("last_bar")} for x in snap["holdings"] if not x.get("pending")],
             "market_condition": {"regime": reg.get("regime"), "reason": reg.get("reason")}, "trades_and_orders": fills, "pending_orders": pend, "top_opportunities_incl_rejected": top,
             "risk_review": {"max_positions": cfg["max_positions"], "max_position_pct": cfg["max_position_pct"], "max_exposure_pct": cfg["max_exposure_pct"], "min_cash_pct": cfg["min_cash_pct"], "risk_per_trade_pct": cfg["risk_per_trade_pct"],
                             "entries_suspended": cfg["entries_suspended"], "max_drawdown_pct_limit": cfg["max_drawdown_pct"], "drawdown_now_pct": k.get("max_drawdown_pct")},
             "benchmark_comparison": {"rows": bm, "note": cmp.get("note")},
             "operational": {"jobs_ok": n_ok, "jobs_failed": n_fail, "jobs_skipped": n_skip, "errors": ev_err, "data_freshness": S.data_freshness(now), "ai_state": aist["state"], "verification": {"status": ver["status"], "streak": ver["consecutive_qualifying"], "required": ver["required_sessions"]}}}
    text, mode, lid = ai.explain("daily_report", "DAILY", facts, "Write a concise end-of-session report (6-9 sentences) from these facts: portfolio value and P&L, what the engine did and why (including rejected opportunities), risk status, benchmark comparison stated plainly (say plainly if underperforming), and operational health. Plain text, no advice, no new numbers.", template_fn=_tmpl)
    body = {"facts": facts, "commentary": text, "commentary_mode": mode, "ai_label": aist["label"], "llm_log_id": lid}
    if store:
        from .. import research; research._store("daily_ai", str(session), body, now)
    return body

def _tmpl(f):
    p = f["portfolio"]; bm = "; ".join(f"{r['key']} {r.get('total_return_pct')}%" for r in f["benchmark_comparison"]["rows"] if r["key"] != "AI")
    return (f"Daily report {ai.TEMPLATE_TAG} for session {f['session']}: equity RM{p['equity_myr']} (USD {p['equity_usd']}), total P&L RM{p['total_pnl_myr']} ({p['total_return_pct']}%), cash USD {p['cash_usd']}, exposure {p['exposure_pct']}%. "
            f"Market regime {f['market_condition']['regime']}. {len(f['trades_and_orders'])} order event(s), {len(f['pending_orders'])} pending. Benchmarks since start: {bm or 'n/a'}. {f['benchmark_comparison']['note']} "
            f"Operations: {f['operational']['jobs_ok']} ok / {f['operational']['jobs_failed']} failed / {f['operational']['jobs_skipped']} skipped jobs; AI state {f['operational']['ai_state']}; verification {f['operational']['verification']['streak']}/{f['operational']['verification']['required']} sessions.")

def latest(n=10):
    c = db.conn(); r = [{"period": x["period"], "ts": x["ts"], **json.loads(x["body"])} for x in c.execute("SELECT * FROM reports WHERE kind='daily_ai' ORDER BY period DESC LIMIT ?", (n,))]; c.close(); return r
