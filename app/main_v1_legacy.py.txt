"""FastAPI backend + static dashboard. No secrets are served to the browser; binds to 127.0.0.1 by default."""
import asyncio, csv, io, json, os, shutil, threading, time
from datetime import datetime, timezone, timedelta
from pathlib import Path
import pandas as pd
from fastapi import FastAPI, HTTPException, Query, Body
from fastapi.responses import StreamingResponse, JSONResponse, Response, FileResponse
from fastapi.staticfiles import StaticFiles
from . import config, db, engine, ledger, data, perf, reports, scoring, calendars as cal, backtest, indicators as ind

app = FastAPI(title="AI Trading Lab (paper trading only)")
STATIC = Path(__file__).parent / "static"
_lock = threading.Lock(); _jobs = {"scan": None, "backtest": None}

# ------------------------------------------------------------ authentication (HTTP Basic, credentials from env/.env)
import base64, secrets
from starlette.middleware.base import BaseHTTPMiddleware

class BasicAuthMiddleware(BaseHTTPMiddleware):
    """Every route (dashboard, /static, /api) requires HTTP Basic. Fails CLOSED (503) if DASH_USER/DASH_PASS are not configured,
    unless AITL_AUTH_DISABLED=1 is set explicitly (local dev only)."""
    async def dispatch(self, request, call_next):
        if os.environ.get("AITL_AUTH_DISABLED") == "1": return await call_next(request)
        u, p = os.environ.get("DASH_USER", ""), os.environ.get("DASH_PASS", "")
        if not u or not p: return Response("auth not configured (set DASH_USER/DASH_PASS)", status_code=503)
        ok = False
        h = request.headers.get("authorization", "")
        if h.lower().startswith("basic "):
            try:
                gu, _, gp = base64.b64decode(h[6:]).decode("utf-8").partition(":")
                ok = secrets.compare_digest(gu.encode(), u.encode()) & secrets.compare_digest(gp.encode(), p.encode())
            except Exception: ok = False
        if not ok: return Response("authentication required", status_code=401, headers={"WWW-Authenticate": 'Basic realm="AI Trading Lab", charset="UTF-8"'})
        return await call_next(request)

app.add_middleware(BasicAuthMiddleware)

@app.on_event("startup")
def _start(): engine.ready()

def now(): return datetime.now(timezone.utc)

def live_provider(): return data.YFProvider()

# ------------------------------------------------------------ status
def _scheduler_health():
    c = db.conn(); r = c.execute("SELECT ts,info FROM heartbeat WHERE name='scheduler'").fetchone(); c.close()
    if not r: return {"healthy": False, "detail": "scheduler process never started", "next_runs": {}}
    age = (now() - datetime.fromisoformat(r["ts"])).total_seconds()
    return {"healthy": age < 120, "last_heartbeat": r["ts"], "age_s": round(age), "next_runs": json.loads(r["info"] or "{}"),
            "detail": "ok" if age < 120 else f"no heartbeat for {int(age)}s - scheduler process is DOWN"}

def _data_mode():
    c = db.conn(); n = c.execute("SELECT COUNT(*) n FROM bars WHERE final=1").fetchone()["n"]; c.close()
    if n == 0: return "DEMO"
    return "LIVE"

@app.get("/api/status")
def status():
    cfg = db.get_settings(); c = db.conn()
    src = [dict(r) for r in c.execute("SELECT * FROM source_status")]
    ls = c.execute("SELECT ts FROM heartbeat WHERE name='last_scan'").fetchone(); c.close()
    mode = _data_mode(); sch = _scheduler_health()
    return {"now_utc": now().isoformat(timespec="seconds"), "now_myt": now().astimezone(ledger.MYT).strftime("%Y-%m-%d %H:%M:%S MYT"),
            "data_mode": mode, "data_label": data.DATA_LABEL,
            "banner": ("DEMO MODE: no live market data loaded - anything shown is synthetic and trading is disabled" if mode == "DEMO" else None),
            "markets": [cal.market_status(m) for m in cal.MARKETS], "fx": data.get_fx(now()), "sources": src, "scheduler": sch,
            "last_scan": ls["ts"] if ls else None, "scan_running": bool(_jobs["scan"] and _jobs["scan"].is_alive()),
            "entries_suspended": cfg["entries_suspended"], "suspend_reason": cfg["suspend_reason"], "auto_trade": cfg["auto_trade"],
            "manual_approval": cfg["manual_approval"], "algo_version": config.ALGO_VERSION, "paper_only": True}

# ------------------------------------------------------------ KPIs / equity
def _principal():
    r = ledger.rows(); return sum(x["cash_delta_myr"] for x in r if x["kind"] in ("DEPOSIT", "WITHDRAW")), (r[0]["cash_delta_myr"] if r else 0)

@app.get("/api/kpis")
def kpis():
    eq = ledger.equity_now(now()); net, start = _principal(); df = perf.curve_df()
    today = now().astimezone(ledger.MYT).date(); prev = df[df.index < pd.Timestamp(today)]
    flows_today = float(df[df.index >= pd.Timestamp(today)]["flows"].sum()) if len(df) else 0.0
    day = eq["equity"] - float(prev["equity"].iloc[-1]) - flows_today if len(prev) else 0.0
    mdd = perf.max_drawdown(perf.nav_series(df)) * 100 if len(df) > 1 else 0.0
    return {"starting_capital": start, "net_contributions": net, "equity": eq["equity"], "cash": eq["cash"], "positions_value": eq["positions_value"],
            "total_pnl": eq["equity"] - net, "todays_pnl": day, "return_pct": (eq["equity"] / net - 1) * 100 if net else 0, "max_drawdown_pct": mdd,
            "exposure_pct": eq["exposure_pct"], "stale_symbols": eq["stale"], "label": "FORWARD PAPER"}

def _bench_series(sym):
    df = data.load_bars(sym, include_partial=False)
    return df["close"] if len(df) else pd.Series(dtype=float)

@app.get("/api/equity")
def equity(range: str = "ALL"):
    df = perf.curve_df()
    if len(df) == 0: return {"equity": [], "benchmark": [], "drawdown": []}
    eq = ledger.equity_now(now()); today = pd.Timestamp(now().astimezone(ledger.MYT).date())
    if df.index[-1] < today: df.loc[today] = [eq["equity"], eq["cash"], eq["positions_value"], 0.0, "", None]
    last = df.index[-1]
    cut = {"1D": 1, "1W": 7, "1M": 30, "3M": 91}.get(range)
    if range == "YTD": df = df[df.index >= pd.Timestamp(last.year, 1, 1)]
    elif cut: df = df[df.index >= last - pd.Timedelta(days=cut)]
    nav = perf.nav_series(df); dd = (nav / nav.cummax() - 1) * 100
    b = _bench_series(db.get_settings()["equity_benchmark"]); bs = []
    if len(b) and len(df):
        bb = b.reindex(df.index, method="ffill").dropna()
        if len(bb): bs = [{"time": str(i.date()), "value": float(v / bb.iloc[0] * df["equity"].iloc[0])} for i, v in bb.items()]
    return {"equity": [{"time": str(i.date()), "value": float(v)} for i, v in df["equity"].items()], "benchmark": bs,
            "benchmark_symbol": db.get_settings()["equity_benchmark"], "benchmark_note": "benchmark rebased to first equity point (price index, local currency)",
            "drawdown": [{"time": str(i.date()), "value": float(v)} for i, v in dd.items()]}

# ------------------------------------------------------------ holdings / discovery
def _latest_decisions(market=None):
    c = db.conn()
    q = "SELECT d.* FROM decisions d JOIN (SELECT symbol, MAX(id) mid FROM decisions WHERE data_mode='LIVE' GROUP BY symbol) m ON d.id=m.mid"
    rows = [dict(r) for r in c.execute(q + (" WHERE d.market=?" if market else ""), (market,) if market else ())]; c.close(); return rows

@app.get("/api/holdings")
def holdings():
    h = ledger.mark_positions(now()); c = db.conn(); out = []
    dec = {d["symbol"]: d for d in _latest_decisions()}
    for p in h:
        ps = c.execute("SELECT * FROM position_state WHERE symbol=?", (p["symbol"],)).fetchone()
        pend = c.execute("SELECT reason FROM orders WHERE symbol=? AND side='SELL' AND status='pending'", (p["symbol"],)).fetchone()
        out.append({**{k: p[k] for k in ("symbol", "market", "qty", "last_price", "last_bar", "fx", "market_value_myr", "unrealized_pnl_myr", "unrealized_pct", "data_flag")},
                    "avg_cost_myr": p["cost_myr"] / p["qty"], "cost_myr": p["cost_myr"], "stop": ps["stop"] if ps else None, "target": ps["target"] if ps else None,
                    "ai_score": dec.get(p["symbol"], {}).get("score"), "action": ("EXIT pending: " + pend["reason"]) if pend else ("HOLD (trailing stop)" if ps else "HOLD (no stop state!)")})
    c.close(); return out

@app.get("/api/discovery")
def discovery(market: str | None = None):
    out = []
    for d in _latest_decisions(market):
        plan = json.loads(d["plan"]) if d["plan"] else None; chk = json.loads(d["checks"]) if d["checks"] else []
        out.append({"id": d["id"], "symbol": d["symbol"], "market": d["market"], "scan_date": d["scan_date"], "score": d["score"], "band": d["band"], "decision": d["decision"],
                    "entry": plan and plan["entry"], "stop": plan and plan["stop"], "target": plan and plan["target"], "rr": plan and plan.get("rr"), "qty": plan and plan["qty"],
                    "failed": [k["name"] for k in chk if not k["pass"]]})
    out.sort(key=lambda x: (-(x["score"] if x["score"] is not None else -1)))
    return out

@app.get("/api/decision/{ref}")
def decision(ref: str):
    c = db.conn()
    r = c.execute("SELECT * FROM decisions WHERE id=?", (int(ref),)).fetchone() if ref.isdigit() else \
        c.execute("SELECT * FROM decisions WHERE symbol=? ORDER BY id DESC LIMIT 1", (ref,)).fetchone()
    if not r: raise HTTPException(404, "no decision")
    d = dict(r)
    for k in ("snapshot", "scores", "checks", "plan"): d[k] = json.loads(d[k]) if d[k] else None
    m = c.execute("SELECT text,ts FROM memos WHERE decision_id=?", (d["id"],)).fetchone(); d["memo"] = dict(m) if m else None
    o = c.execute("SELECT * FROM orders WHERE decision_id=?", (d["id"],)).fetchone(); d["order"] = dict(o) if o else None
    c.close(); return d

# ------------------------------------------------------------ charts
@app.get("/api/candles/{symbol}")
def candles(symbol: str, bars: int = 260):
    df = data.load_bars(symbol, include_partial=True); demo = False
    if len(df) == 0:
        if _data_mode() == "DEMO":
            df = data.DemoProvider().daily([symbol])[symbol]; df["final"] = 1; demo = True
        else: raise HTTPException(404, f"no data for {symbol} (not collected yet)")
    bench = _bench_series(db.get_settings()["benchmarks"][cal.market_of(symbol)])
    f = scoring.feature_frame(df, bench if len(bench) else None).tail(bars)
    T = lambda i: str(i.date())
    ser = lambda col: [{"time": T(i), "value": float(v)} for i, v in f[col].items() if pd.notna(v)]
    rows = [r for r in ledger.rows() if r["symbol"] == symbol]
    markers = [{"time": T(pd.Timestamp(ledger.myt_date(r["ts"]))), "position": "belowBar" if r["kind"] == "BUY" else "aboveBar", "color": "#26a69a" if r["kind"] == "BUY" else "#ef5350",
                "shape": "arrowUp" if r["kind"] == "BUY" else "arrowDown", "text": f"{r['kind']} {r['qty']}@{r['price_local']:.3f}"} for r in rows]
    c = db.conn(); ps = c.execute("SELECT * FROM position_state WHERE symbol=?", (symbol,)).fetchone(); c.close()
    st = data.data_status(symbol, now())
    return {"symbol": symbol, "demo": demo, "status": st, "label": ("DEMO SYNTHETIC DATA" if demo else data.DATA_LABEL),
            "candles": [{"time": T(i), "open": float(r.open), "high": float(r.high), "low": float(r.low), "close": float(r.close)} for i, r in f.iterrows()],
            "volume": [{"time": T(i), "value": float(r.volume), "color": "#26a69a55" if r.close >= r.open else "#ef535055"} for i, r in f.iterrows()],
            "sma20": ser("sma20"), "sma50": ser("sma50"), "sma200": ser("sma200"), "ema20": ser("ema20"), "rsi": ser("rsi14"), "macd": ser("macd"), "macd_sig": ser("macd_sig"),
            "macd_hist": [{"time": T(i), "value": float(v), "color": "#26a69a" if v >= 0 else "#ef5350"} for i, v in f["macd_hist"].items() if pd.notna(v)],
            "markers": markers, "stop": ps["stop"] if ps else None, "target": ps["target"] if ps else None}

# ------------------------------------------------------------ ledger / export
def _ledger_rows():
    out = []
    for r in ledger.rows():
        r = dict(r); r["ts_myt"] = datetime.fromisoformat(r["ts"]).astimezone(ledger.MYT).strftime("%Y-%m-%d %H:%M:%S"); out.append(r)
    return out

@app.get("/api/ledger")
def ledger_api(): return list(reversed(_ledger_rows()))

COLS = ["id", "ts_myt", "kind", "symbol", "market", "qty", "price_local", "ccy", "fx", "gross_myr", "fees_myr", "slippage_myr", "cash_delta_myr", "realized_pnl_myr", "order_id", "note"]

@app.get("/api/export/ledger.csv")
def export_csv():
    s = io.StringIO(); w = csv.writer(s); w.writerow(COLS)
    for r in _ledger_rows(): w.writerow([r.get(k) for k in COLS])
    return Response(s.getvalue(), media_type="text/csv", headers={"Content-Disposition": "attachment; filename=ai_trading_lab_ledger.csv"})

@app.get("/api/export/ledger.xlsx")
def export_xlsx():
    from openpyxl import Workbook
    wb = Workbook(); ws = wb.active; ws.title = "Ledger"; ws.append(COLS)
    for r in _ledger_rows(): ws.append([r.get(k) for k in COLS])
    ws2 = wb.create_sheet("Orders"); c = db.conn(); rs = c.execute("SELECT * FROM orders ORDER BY id").fetchall(); c.close()
    if rs: ws2.append(list(rs[0].keys())); [ws2.append(list(r)) for r in rs]
    ws3 = wb.create_sheet("Holdings"); h = holdings(); ws3.append(["symbol", "qty", "avg_cost_myr", "last_price", "market_value_myr", "unrealized_pnl_myr", "stop", "ai_score"])
    for x in h: ws3.append([x[k] for k in ["symbol", "qty", "avg_cost_myr", "last_price", "market_value_myr", "unrealized_pnl_myr", "stop", "ai_score"]])
    ws4 = wb.create_sheet("Performance"); [ws4.append([k, json.dumps(v) if isinstance(v, (dict, list)) else v]) for k, v in perf.metrics().items()]
    b = io.BytesIO(); wb.save(b)
    return Response(b.getvalue(), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", headers={"Content-Disposition": "attachment; filename=ai_trading_lab.xlsx"})

@app.get("/api/orders")
def orders():
    c = db.conn(); r = [dict(x) for x in c.execute("SELECT * FROM orders ORDER BY id DESC LIMIT 200")]; c.close(); return r

# ------------------------------------------------------------ analytics
@app.get("/api/performance")
def performance(): return perf.metrics()

@app.get("/api/analytics")
def analytics():
    rws = ledger.rows(); pos, trades = ledger.replay(rws); eq = ledger.equity_now(now())
    by = {}
    for t in trades: by[t["symbol"]] = by.get(t["symbol"], 0) + t["pnl_myr"]
    for h in eq["holdings"]: by[h["symbol"]] = by.get(h["symbol"], 0) + h["unrealized_pnl_myr"]
    alloc = [{"name": "CASH", "value": eq["cash"]}] + [{"name": h["symbol"], "value": h["market_value_myr"]} for h in eq["holdings"]]
    cum_f = cum_s = 0; fs = []
    for r in rws:
        if r["kind"] in ("BUY", "SELL"):
            cum_f += r["fees_myr"] or 0; cum_s += r["slippage_myr"] or 0; fs.append({"time": ledger.myt_date(r["ts"]).isoformat(), "fees": cum_f, "slippage": cum_s})
    return {"monthly": perf.periodic_returns("M"), "weekly": perf.periodic_returns("W"),
            "win_loss": {"wins": len([t for t in trades if t["pnl_myr"] > 0]), "losses": len([t for t in trades if t["pnl_myr"] <= 0])},
            "profit_by_symbol": [{"name": k, "value": v} for k, v in sorted(by.items(), key=lambda x: -x[1])], "allocation": alloc, "fees_slippage": fs}

# ------------------------------------------------------------ feed / SSE
@app.get("/api/feed")
def feed(after: int = 0, limit: int = 100):
    c = db.conn(); r = [dict(x) for x in c.execute("SELECT * FROM events WHERE id>? ORDER BY id DESC LIMIT ?", (after, limit))]; c.close(); return list(reversed(r))

@app.get("/api/stream")
async def stream(after: int = 0):
    """Server-sent events: new activity rows + periodic status ticks. Frontend falls back to polling /api/feed on error."""
    async def gen():
        last = after; i = 0
        yield "retry: 5000\n\n"
        while True:
            for ev in feed(last, 50): last = max(last, ev["id"]); yield f"event: activity\ndata: {json.dumps(ev)}\n\n"
            if i % 5 == 0: yield f"event: tick\ndata: {json.dumps({'now': now().isoformat(timespec='seconds')})}\n\n"
            i += 1; await asyncio.sleep(3)
    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

@app.get("/api/reports/{kind}")
def get_report(kind: str):
    c = db.conn(); r = c.execute("SELECT body FROM reports WHERE kind=? ORDER BY period DESC LIMIT 1", (kind,)).fetchone(); c.close()
    if kind == "daily" and not r: return reports.daily_report(now(), store=False)
    if not r: raise HTTPException(404, "none yet")
    return json.loads(r["body"])

# ------------------------------------------------------------ actions
def _run_scan(market):
    with _lock:
        try: engine.scan(live_provider(), now(), market=market)
        except Exception as e: db.event("ERROR", "scan", f"manual scan failed: {e}")

@app.post("/api/scan")
def scan(market: str | None = None):
    if _jobs["scan"] and _jobs["scan"].is_alive(): raise HTTPException(409, "scan already running")
    t = threading.Thread(target=_run_scan, args=(market,), daemon=True); t.start(); _jobs["scan"] = t
    return {"started": True, "market": market or "all enabled"}

@app.post("/api/monitor")
def monitor():
    return {"log": engine.execute_pending(live_provider(), now())}

@app.post("/api/orders/{oid}/approve")
def approve(oid: int): engine.approve_order(oid); return {"ok": True}

@app.post("/api/orders/{oid}/cancel")
def cancel(oid: int): engine.cancel_order(oid); return {"ok": True}

@app.post("/api/resume")
def resume(body: dict = Body(...)):
    if body.get("confirm") != "RESUME": raise HTTPException(400, "confirm must equal 'RESUME'")
    engine.resume_entries(); return {"ok": True}

@app.get("/api/settings")
def get_settings(): return db.get_settings()

@app.put("/api/settings")
def put_settings(body: dict = Body(...)):
    cur = db.get_settings(); clean = {}
    for k, v in body.items():
        if k not in config.DEFAULTS: raise HTTPException(400, f"unknown setting {k}")
        if k in ("entries_suspended", "suspend_reason", "dd_baseline_ts"): raise HTTPException(400, f"{k} is controlled by the risk engine (use /api/resume)")
        if type(v) != type(cur[k]) and not (isinstance(v, (int, float)) and isinstance(cur[k], (int, float)) and not isinstance(v, bool)): raise HTTPException(400, f"bad type for {k}")
        clean[k] = v
    for k in ("risk_per_trade_pct", "max_position_pct", "max_exposure_pct", "min_cash_pct", "max_drawdown_pct"):
        if k in clean and not (0 < clean[k] <= 100): raise HTTPException(400, f"{k} must be in (0,100]")
    if "risk_per_trade_pct" in clean and clean["risk_per_trade_pct"] > 5: raise HTTPException(400, "risk_per_trade_pct capped at 5 in this paper system")
    db.set_settings(clean); return db.get_settings()

@app.post("/api/cash")
def cash(body: dict = Body(...)):
    kind = body.get("kind", "").upper(); amt = float(body.get("amount", 0))
    if kind not in ("DEPOSIT", "WITHDRAW") or amt <= 0: raise HTTPException(400, "kind DEPOSIT|WITHDRAW and positive amount required")
    try: ledger.cash_flow(kind, amt, now(), body.get("note", "manual (virtual)"))
    except ValueError as e: raise HTTPException(400, str(e))
    engine.reconcile(now()); return kpis()

@app.post("/api/override")
def override(body: dict = Body(...)):
    act, sym = body.get("action"), body.get("symbol", "").upper(); cfg = db.get_settings()
    if act == "block":
        db.set_settings({"blocked_symbols": sorted(set(cfg["blocked_symbols"]) | {sym})})
    elif act == "unblock":
        db.set_settings({"blocked_symbols": [s for s in cfg["blocked_symbols"] if s != sym]})
    elif act == "close":
        try: return {"order_id": engine.manual_close(sym, now()), "note": "SELL pending; fills at next session open"}
        except ValueError as e: raise HTTPException(400, str(e))
    elif act == "raise_stop":
        c = db.conn(); ps = c.execute("SELECT stop FROM position_state WHERE symbol=?", (sym,)).fetchone()
        new = float(body["stop"])
        if not ps or new <= ps["stop"]: raise HTTPException(400, "stops may only be raised")
        c.execute("UPDATE position_state SET stop=? WHERE symbol=?", (new, sym)); c.commit(); c.close()
    else: raise HTTPException(400, "unknown action")
    db.event("WARN", "override", f"manual override {act} {sym}"); return {"ok": True}

@app.post("/api/reset")
def reset(body: dict = Body(...)):
    if body.get("confirm") != "RESET": raise HTTPException(400, "confirm must equal 'RESET'")
    p = Path(config.db_path()); arch = p.parent / "archive"; arch.mkdir(exist_ok=True)
    stamp = now().strftime("%Y%m%d-%H%M%S"); keep = db.get_settings()
    for suf in ("", "-wal", "-shm"):
        f = Path(str(p) + suf)
        if f.exists(): shutil.move(str(f), str(arch / f"{p.stem}-{stamp}.db{suf[1:] and '-' + suf[1:]}"))
    db.init(); db.set_settings({k: v for k, v in keep.items() if k not in ("entries_suspended", "suspend_reason", "dd_baseline_ts")})
    if body.get("capital"): db.set_settings({"starting_capital": float(body["capital"])})
    ledger.init_account(); db.event("WARN", "reset", f"FULL RESET performed; old database archived as archive/{p.stem}-{stamp}.db")
    return {"ok": True, "archived": f"archive/{p.stem}-{stamp}.db"}

@app.get("/api/export/settings")
def export_settings(): return db.get_settings()

@app.post("/api/import/settings")
def import_settings(body: dict = Body(...)):
    body = {k: v for k, v in body.items() if k not in ("entries_suspended", "suspend_reason", "dd_baseline_ts")}
    try: db.set_settings(body)
    except KeyError as e: raise HTTPException(400, str(e))
    return db.get_settings()

@app.get("/api/backtest")
def backtest_latest():
    c = db.conn(); r = c.execute("SELECT result FROM backtests ORDER BY id DESC LIMIT 1").fetchone(); c.close()
    running = bool(_jobs["backtest"] and _jobs["backtest"].is_alive())
    return {"running": running, "result": json.loads(r["result"]) if r else None}

def _run_bt():
    try:
        res = backtest.run(); backtest.save(res); db.event("INFO", "backtest", "backtest finished")
    except Exception as e: db.event("ERROR", "backtest", f"backtest failed: {e}")

@app.post("/api/backtest")
def backtest_run():
    if _jobs["backtest"] and _jobs["backtest"].is_alive(): raise HTTPException(409, "running")
    t = threading.Thread(target=_run_bt, daemon=True); t.start(); _jobs["backtest"] = t; return {"started": True}

app.mount("/static", StaticFiles(directory=STATIC), name="static")

@app.get("/favicon.ico", include_in_schema=False)
def favicon(): return Response(status_code=204)

@app.get("/")
def index(): return FileResponse(STATIC / "index.html")
