"""Read models (JSON-ready dicts) shared by the FastAPI backend and the static exporter. Derived from DB only; nothing here can trade."""
import json
from datetime import datetime, timezone, timedelta, date
import numpy as np, pandas as pd
from . import db, config, data, ledger, portfolio, fx as fxm, calendars as cal, perf, universe
from .strategies import common as C, intraday
from .providers import registry
from . import ai

STRATEGY_NAMES = {"swing": "Momentum & Swing", "meanrev": "Mean reversion", "baseline": "Buy & hold SPY/QQQ", "intraday": "Intraday (proxy research)"}
def now(): return datetime.now(timezone.utc)
def _j(x, d=None):
    try: return json.loads(x) if x else d
    except Exception: return d

def curve(strategy="ALL"):
    c = db.conn(); r = [dict(x) for x in c.execute("SELECT * FROM equity_curve2 WHERE strategy_id=? ORDER BY date", (strategy,))]; c.close(); return r

def fx_status(cfg=None): return fxm.current(cfg)

def kpis(cfg=None, n=None):
    n = n or now(); cfg = cfg or db.get_settings(); snap = portfolio.snapshot(n, cfg); cv = curve("ALL")
    fx = snap["fx"].get("rate"); today = n.astimezone(ledger.MYT).date().isoformat()
    eq = snap["equity_myr"]; start = cfg["start_capital_myr"]
    rows = ledger.rows(); net = sum(r["cash_delta_myr"] for r in rows if r["kind"] in ("DEPOSIT", "WITHDRAW")) or start
    prev = [r for r in cv if r["date"] < today]; day = eq - prev[-1]["equity_myr"] if prev else None
    def ret_since(days):
        if len(cv) < 2: return None
        cutoff = (n.astimezone(ledger.MYT).date() - timedelta(days=days)).isoformat(); base = [r for r in cv if r["date"] <= cutoff]
        b = base[-1]["equity_myr"] if base else cv[0]["equity_myr"]; return (eq / b - 1) * 100
    s = pd.Series({r["date"]: r["equity_myr"] for r in cv}); mdd = float((s / s.cummax() - 1).min() * 100) if len(s) > 1 else 0.0
    sl = snap["sleeves"]; active = [k for k, v in cfg["strategies"].items() if v["enabled"] and k != "intraday"]
    legacy_cash = snap["cash_myr"]; usd_cash = sum(x["cash_usd"] for x in sl.values()); settled = sum(x["settled_cash_usd"] for x in sl.values()); uns = sum(x["unsettled_cash_usd"] for x in sl.values())
    return {"starting_capital_myr": start, "net_contributions_myr": net, "equity_myr": eq, "equity_usd": snap["equity_usd"], "usdmyr": fx, "fx": snap["fx"],
            "cash_myr": legacy_cash, "cash_usd": usd_cash, "settled_cash_usd": settled, "unsettled_cash_usd": uns, "cash_total_myr": legacy_cash + (usd_cash * fx if fx else 0),
            "positions_myr": sum(h["value_myr"] for h in snap["holdings"] if not h.get("pending")), "today_pnl_myr": day, "total_pnl_myr": eq - net, "total_return_pct": (eq / net - 1) * 100 if net else None,
            "weekly_return_pct": ret_since(7), "monthly_return_pct": ret_since(30), "max_drawdown_pct": mdd, "active_positions": len([h for h in snap["holdings"] if not h.get("pending")]),
            "active_strategies": active, "n_active_strategies": len(active), "us_market": cal.us_status(n), "label": "FORWARD PAPER (simulated fills, delayed yfinance data)",
            "fx_spread_paid_myr": sum(x["fx_spread_paid_myr"] for x in sl.values()), "stock_pnl_myr": sum(x["stock_pnl_myr"] for x in sl.values()), "currency_pnl_myr": sum(x["currency_pnl_myr"] for x in sl.values()),
            "history_days": len(cv), "note": "returns need >=2 daily points; today's P&L is vs the previous recorded close"}

def _bench(sym, dates, fxd):
    df = data.load_bars(sym, include_partial=False)
    if len(df) == 0 or not dates: return []
    out = []; base = None
    for d in dates:
        px = df[df.index <= pd.Timestamp(d)]; f = fxd.get(d)
        if len(px) == 0 or not f: continue
        v = float(px["close"].iloc[-1]) * f; base = base or v; out.append({"time": d, "value": v / base})
    return out

def equity_curves(cfg=None):
    cfg = cfg or db.get_settings(); allc = curve("ALL"); dates = [r["date"] for r in allc]; fxd = {r["date"]: r["fx"] for r in allc}
    inv = {}
    for r in ledger.rows():
        if r["kind"] == "FX_CONVERT": inv[r["strategy_id"]] = inv.get(r["strategy_id"], 0) + r["gross_myr"]
    series = {"portfolio": [{"time": r["date"], "value": r["equity_myr"] / (cfg["start_capital_myr"]) } for r in allc]}
    for sid in ("swing", "meanrev", "baseline"):
        cv = curve(sid); base = inv.get(sid)
        series[sid] = [{"time": r["date"], "value": r["equity_myr"] / base} for r in cv] if base else []
    series["intraday"] = []
    series["SPY"] = _bench("SPY", dates, fxd); series["QQQ"] = _bench("QQQ", dates, fxd)
    return {"dates": dates, "series": series, "unit": "index, 1.00 = amount invested (strategy sleeves) / RM10,000 (portfolio); all in MYR at each day's mid USD/MYR (SPY/QQQ include the FX move, no FX spread)",
            "intraday_note": "DISABLED: no real-time intraday feed detected - no forward intraday results exist", "label": "FORWARD PAPER ONLY - backtests are never mixed into this chart"}

def holdings(cfg=None):
    cfg = cfg or db.get_settings(); snap = portfolio.snapshot(now(), cfg); out = []; c = db.conn(); fx = snap["fx"].get("rate") or 0
    for h in snap["holdings"]:
        if h.get("pending"): continue
        ps = c.execute("SELECT * FROM position_state2 WHERE strategy_id=? AND symbol=?", (h["strategy_id"], h["symbol"])).fetchone()
        pend = c.execute("SELECT reason FROM orders WHERE symbol=? AND strategy_id=? AND side='SELL' AND status='pending'", (h["symbol"], h["strategy_id"])).fetchone()
        days = (now() - datetime.fromisoformat(h["entry_ts"])).days
        out.append({**h, "unrealized_myr": h["unrealized_usd"] * fx, "unrealized_pct": h["unrealized_usd"] / h["cost_usd"] * 100 if h["cost_usd"] else 0, "days_held": days, "stop": ps["stop"] if ps else None,
                    "target": ps["target"] if ps else None, "reason": ps["reason"] if ps else None, "action": ("EXIT pending: " + pend["reason"]) if pend else "HOLD"})
    c.close(); return out

def orders(limit=60):
    c = db.conn(); r = [dict(x) for x in c.execute("SELECT id,created_ts,updated_ts,symbol,side,qty,ref_price,stop,target,status,reason,needs_approval,approved,fill_price,fill_ts,strategy_id,signal_status FROM orders WHERE market!='BURSA' ORDER BY id DESC LIMIT ?", (limit,))]; c.close(); return r

def ledger_rows(limit=500):
    cols = ["id", "ts", "kind", "strategy_id", "symbol", "qty", "price_local", "ccy", "fx_basis", "gross_usd", "fees_usd", "spread_usd", "slip_usd", "cash_delta_myr", "cash_delta_usd", "realized_stock_usd", "realized_pnl_myr", "settle_date", "order_id", "note"]
    rows = ledger.rows(); out = [{k: r.get(k) for k in cols} for r in rows]; return list(reversed(out))[:limit]

def fx_conversions():
    c = db.conn(); r = [dict(x) for x in c.execute("SELECT * FROM fx_conversions ORDER BY id DESC")]; c.close(); return r

def discovery():
    """Merged per-symbol table: swing score/status, mean-reversion status, universe metrics. Latest scan only."""
    c = db.conn(); sr = {r["strategy_id"]: (r["scan_date"], _j(r["payload"], {})) for r in c.execute("SELECT * FROM scan_results WHERE scan_date=(SELECT MAX(scan_date) FROM scan_results)")}
    um = {r["symbol"]: dict(r) for r in c.execute("SELECT * FROM universe_members")}; c.close()
    rows = {}
    for sid, (sd, p) in sr.items():
        for r in p.get("rows", []):
            e = rows.setdefault(r["symbol"], {"symbol": r["symbol"], "scan_date": sd}); e[sid] = {"status": r.get("status"), "score": r.get("score"), "failed": r.get("failed"), "plan": r.get("plan"), "id": r.get("id"), "order_id": r.get("order_id")}
            if sid == "swing": e.update({k: r.get(k) for k in ("close", "rsi14", "r126", "prox52", "rs_spy126", "rs_qqq126", "vol_ratio", "adv_usd", "factors")})
            if sid == "meanrev": e.setdefault("close", r.get("close")); e["rsi2"] = r.get("rsi2")
    out = []
    for s, e in rows.items():
        u = um.get(s, {}); e["name"] = u.get("name"); e["exchange"] = u.get("exchange"); e["sources"] = u.get("sources"); out.append(e)
    out.sort(key=lambda e: -((e.get("swing") or {}).get("score") or -1))
    for e in out[:200]:                                    # v3: price context from stored FINAL daily bars (END-OF-DAY, never live)
        try:
            df = data.load_bars(e["symbol"], include_partial=False)
            if df is not None and len(df) >= 2:
                cl = df["close"]; e["d1_pct"] = float((cl.iloc[-1] / cl.iloc[-2] - 1) * 100); e["volume"] = float(df["volume"].iloc[-1]); e["last_bar"] = str(df.index[-1].date()); e["price_label"] = "END-OF-DAY"
                s50 = cl.rolling(50).mean().iloc[-1]; s200 = cl.rolling(200).mean().iloc[-1] if len(cl) >= 200 else None
                e["trend"] = ("UPTREND" if cl.iloc[-1] > s50 and (s200 is None or s50 > s200) else "DOWNTREND" if cl.iloc[-1] < s50 and (s200 is not None and s50 < s200) else "MIXED") if s50 == s50 else None
        except Exception: pass
        sw = e.get("swing") or {}; e["strategy"] = "Momentum & Swing"; e["action"] = sw.get("status")
    return {"rows": out, "regime": (list(sr.values())[0][1].get("regime") if sr else None), "scan_date": list(sr.values())[0][0] if sr else None}

def universe_summary():
    c = db.conn(); tot = c.execute("SELECT COUNT(*) n, SUM(passed) p FROM universe_members").fetchone()
    rej = [dict(r) for r in c.execute("SELECT symbol,exchange,sources,reasons FROM universe_members WHERE passed=0 ORDER BY symbol")]
    cache = [dict(r) for r in c.execute("SELECT key,fetched_at FROM universe_cache")]; c.close()
    return {"candidates": tot["n"], "passed": tot["p"], "rejected": rej[:120], "sources": cache, "filters": db.get_settings()["universe_filters"],
            "method": "S&P 500 (Wikipedia) + Nasdaq-100 (Nasdaq API/Wikipedia) constituents; exchange from NASDAQ Trader directory; filters on real daily bars. Current constituents => survivorship bias in backtests."}

def candles(symbol, bars=260):
    df = data.load_bars(symbol, include_partial=True)
    if len(df) == 0: return None
    from . import scoring
    f = scoring.enrich(scoring.feature_frame(df, None), df)
    f = f.tail(bars); idx = [str(i.date()) for i in f.index]
    tp = (df["high"] + df["low"] + df["close"]) / 3; vw = ((tp * df["volume"]).rolling(20).sum() / df["volume"].rolling(20).sum()).reindex(f.index)
    col = lambda s: [None if pd.isna(v) else round(float(v), 4) for v in s]
    mk = []
    for r in ledger.rows():
        if r.get("symbol") == symbol and r["kind"] in ("BUY", "SELL") and r.get("strategy_id"): mk.append({"time": ledger.myt_date(r["ts"]).isoformat() if False else r["ts"][:10], "side": r["kind"], "price": r["price_local"], "qty": r["qty"], "strategy": r["strategy_id"]})
    return {"symbol": symbol, "last_bar": idx[-1], "final": bool(df["final"].iloc[-1]), "label": "yfinance daily bars (EOD/delayed). 'VWAP' = 20-day rolling VWAP from daily OHLCV (PROXY).",
            "candles": [{"time": idx[i], "open": round(float(f["open"].iloc[i]), 4), "high": round(float(f["high"].iloc[i]), 4), "low": round(float(f["low"].iloc[i]), 4), "close": round(float(f["close"].iloc[i]), 4), "volume": float(f["volume"].iloc[i])} for i in range(len(f))],
            "sma20": col(f["sma20"]), "sma50": col(f["sma50"]), "sma200": col(f["sma200"]), "vwap20": col(vw), "rsi14": col(f["rsi14"]), "macd": col(f["macd"]), "macd_sig": col(f["macd_sig"]), "macd_hist": col(f["macd_hist"]), "markers": mk}

def decision_for(symbol):
    c = db.conn(); out = {"symbol": symbol, "strategies": {}}
    for sid in ("swing", "meanrev", "baseline"):
        r = c.execute("SELECT * FROM decisions WHERE symbol=? AND strategy_id=? ORDER BY id DESC LIMIT 1", (symbol, sid)).fetchone()
        if r:
            d = dict(r)
            for k in ("snapshot", "scores", "checks", "plan"): d[k] = _j(d[k])
            m = c.execute("SELECT * FROM memos2 WHERE symbol=? AND strategy_id=? ORDER BY id DESC LIMIT 1", (symbol, sid)).fetchone()
            d["memo"] = {**dict(m), "fields": _j(m["fields"])} if m else None
            o = c.execute("SELECT id,status,reason,qty FROM orders WHERE decision_id=?", (d["id"],)).fetchone(); d["order"] = dict(o) if o else None
            out["strategies"][sid] = d
    c.close(); return out

def strategies_view(cfg=None):
    cfg = cfg or db.get_settings(); n = now(); snap = portfolio.snapshot(n, cfg); caps = registry.results_from_store(); cv = curve_all_rows(); out = []
    for sid in ("swing", "meanrev", "baseline", "intraday"):
        st = cfg["strategies"][sid]; sl = snap["sleeves"].get(sid)
        m = C.summary_metrics(sid, snap, snap["trades"], cv, cfg["risk_free_rate_annual"]) if sid != "intraday" else {"strategy_id": sid, "n_trades": 0}
        row = {"id": sid, "name": STRATEGY_NAMES[sid], "enabled": st["enabled"], "allocation_pct": st["allocation_pct"], "sleeve": sl, "metrics": m}
        if sid == "intraday":
            s = intraday.status(cfg, caps); row.update({"enabled_effective": s["live_enabled"], "research_enabled": s["research_enabled"], "disabled_reasons": s["reasons"], "label": s["label"]})
        else:
            c = db.conn(); r = c.execute("SELECT suspended,reason FROM strategy_state WHERE strategy_id=?", (sid,)).fetchone(); c.close()
            row["enabled_effective"] = bool(st["enabled"] and not (r and r["suspended"])); row["suspended_reason"] = r["reason"] if r and r["suspended"] else None
        out.append(row)
    return out

def curve_all_rows():
    c = db.conn(); r = [dict(x) for x in c.execute("SELECT * FROM equity_curve2")]; c.close(); return r

def monitor_view():
    c = db.conn(); hb = {r["name"]: dict(r) for r in c.execute("SELECT * FROM heartbeat")}; src = [dict(r) for r in c.execute("SELECT * FROM source_status")]
    errs = [dict(r) for r in c.execute("SELECT ts,level,source,message FROM events WHERE level IN ('ERROR','CRITICAL','WARN') ORDER BY id DESC LIMIT 20")]
    ev = [dict(r) for r in c.execute("SELECT ts,level,source,message FROM events ORDER BY id DESC LIMIT 60")]; c.close()
    n = now(); us = cal.us_status(n)
    return {"heartbeats": hb, "sources": src, "errors": errs, "events": ev, "us_market": us, "fx": fx_status(),
            "scheduler": "GitHub Actions cron (best effort; every ~15 min during US hours, scan after close) - NOT real-time ticks",
            "freshness": data_freshness()}

def data_freshness():
    spy = data.load_bars("SPY", include_partial=True)
    return {"spy_last_bar": str(spy.index[-1].date()) if len(spy) else None, "expected_last_completed_session": str(cal.last_completed_session("US", now())), "label": data.DATA_LABEL}

def feed(limit=60):
    c = db.conn(); memos = [{**{k: r[k] for k in ("ts", "symbol", "strategy_id", "mode", "text", "decision_id")}, "kind": "memo", "fields": _j(r["fields"])} for r in c.execute("SELECT * FROM memos2 ORDER BY id DESC LIMIT ?", (limit,))]
    llm = [dict(r) for r in c.execute("SELECT id,ts,kind,symbol,provider,model,mode,ok,error,substr(response,1,1500) response FROM llm_log WHERE kind IN ('market_summary','weekly_report') ORDER BY id DESC LIMIT 6")]
    dec = [dict(r) for r in c.execute("SELECT id,ts,symbol,strategy_id,decision,score,substr(explanation,1,300) explanation FROM decisions WHERE strategy_id IS NOT NULL AND decision IN ('BUY ELIGIBLE','EXIT') ORDER BY id DESC LIMIT 40")]; c.close()
    return {"memos": memos, "summaries": llm, "decisions": dec, "ai": ai.status(), "note": "LLM explains verified data only; the deterministic engine decides and executes."}
