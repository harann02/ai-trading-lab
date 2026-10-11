"""Research outputs: AI memos (verified-data explanations), daily market summary, weekly strategy report. Failures never affect trading."""
import json, hashlib
from datetime import datetime, timezone, timedelta
import pandas as pd
from . import db, data, ledger, portfolio, ai, views, calendars as cal, fx as fxm
from .strategies import common as C

def _store(kind, period, body, now):
    h = hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()
    c = db.conn(); c.execute("INSERT INTO reports(kind,period,ts,hash,body) VALUES(?,?,?,?,?) ON CONFLICT(kind,period) DO UPDATE SET ts=excluded.ts,hash=excluded.hash,body=excluded.body",
                             (kind, period, ledger.iso(now), h, json.dumps(body, default=str))); c.commit(); c.close()

def generate_memos(provider, now=None, max_memos=10):
    now = now or datetime.now(timezone.utc); c = db.conn(); out = []
    sd = c.execute("SELECT MAX(scan_date) d FROM decisions WHERE strategy_id IS NOT NULL").fetchone()["d"]
    if not sd: c.close(); return out
    rows = [dict(r) for r in c.execute("SELECT * FROM decisions WHERE strategy_id IS NOT NULL AND scan_date=? AND decision IN ('BUY ELIGIBLE','EXIT') ORDER BY score DESC", (sd,))]
    rows += [dict(r) for r in c.execute("SELECT * FROM decisions WHERE strategy_id='swing' AND scan_date=? AND decision='WATCH' ORDER BY score DESC LIMIT 3", (sd,))]
    have = {(r["decision_id"]) for r in c.execute("SELECT decision_id FROM memos2")}
    um = {r["symbol"]: r["name"] for r in c.execute("SELECT symbol,name FROM universe_members")}; c.close()
    for d in rows[:max_memos + len(have & {r['id'] for r in rows})]:
        if d["id"] in have or len(out) >= max_memos: continue
        c = db.conn(); o = c.execute("SELECT * FROM orders WHERE decision_id=?", (d["id"],)).fetchone(); c.close()
        try: news = provider.news(d["symbol"], 6)
        except Exception: news = []
        try: out.append(ai.make_memo(d, dict(o) if o else None, news, um.get(d["symbol"]), views.STRATEGY_NAMES.get(d["strategy_id"], d["strategy_id"])))
        except Exception as e: db.event("WARN", "ai", f"memo failed for {d['symbol']}: {e}", ledger.iso(now))
    return out

def _ret1(sym):
    df = data.load_bars(sym, include_partial=False)
    return (float(df["close"].iloc[-1]), float((df["close"].iloc[-1] / df["close"].iloc[-2] - 1) * 100)) if len(df) > 2 else (None, None)

def daily_market_summary(now=None):
    now = now or datetime.now(timezone.utc); k = views.kpis(n=now); d = views.discovery(); spy, spyr = _ret1("SPY"); qqq, qqqr = _ret1("QQQ")
    counts = {}
    for r in d["rows"]:
        for sid in ("swing", "meanrev"):
            s = (r.get(sid) or {}).get("status")
            if s: counts.setdefault(sid, {}); counts[sid][s] = counts[sid].get(s, 0) + 1
    reg = d.get("regime") or {}
    facts = {"date_myt": now.astimezone(ledger.MYT).strftime("%Y-%m-%d"), "session": d.get("scan_date"), "regime": reg.get("regime"), "regime_reason": reg.get("reason"), "spy_close": round(spy, 2) if spy else None,
             "spy_ret_1d_pct": round(spyr, 2) if spyr is not None else None, "qqq_close": round(qqq, 2) if qqq else None, "qqq_ret_1d_pct": round(qqq, 2) if False else (round(qqqr, 2) if qqqr is not None else None),
             "usdmyr": round(k["usdmyr"], 4) if k["usdmyr"] else None, "fx_date": (k["fx"] or {}).get("rate_ts"), "universe_passed": views.universe_summary()["passed"], "signal_counts": counts, "equity_myr": round(k["equity_myr"], 2)}
    text, mode, lid = ai.market_summary(facts)
    body = {"facts": facts, "text": text, "mode": mode, "llm_log_id": lid}; _store("market_summary", facts["date_myt"], body, now); return body

def weekly_strategy_report(now=None):
    now = now or datetime.now(timezone.utc); sv = views.strategies_view(); wk = {}
    n_all = 0
    for s in sv:
        m = s["metrics"]; sl = s["sleeve"] or {}
        wk[s["id"]] = {"return_on_allocated_pct": round(m.get("return_on_allocated_pct"), 3) if m.get("return_on_allocated_pct") is not None else None, "n_trades": m.get("n_trades"), "win_rate": m.get("win_rate"),
                       "max_drawdown_pct": m.get("max_drawdown_pct"), "costs_usd": round((m.get("fees_usd") or 0) + (m.get("spread_slippage_usd") or 0), 3), "fx_spread_myr": m.get("fx_spread_myr")}
        n_all += m.get("n_trades") or 0
    def wret(sym):
        df = data.load_bars(sym, include_partial=False); return round(float((df["close"].iloc[-1] / df["close"].iloc[-6] - 1) * 100), 2) if len(df) > 6 else None
    facts = {"week_ending_myt": now.astimezone(ledger.MYT).strftime("%Y-%m-%d"), "strategies": wk, "spy_week_pct": wret("SPY"), "qqq_week_pct": wret("QQQ"),
             "sample_note": f"{n_all} closed trades in total: far below the evidence needed to call any strategy better; no winner is declared (minimum 30 trades per strategy and an out-of-sample period)."}
    text, mode, lid = ai.weekly_report(facts); body = {"facts": facts, "text": text, "mode": mode, "llm_log_id": lid}
    _store("weekly_strategy", facts["week_ending_myt"], body, now); return body
