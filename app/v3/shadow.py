"""Shadow benchmark portfolios: SEPARATE virtual SPY and QQQ portfolios (RM10,000 each), never touching trading cash and never in the ledger.
METHOD (documented, deterministic, rebuilt from stored data):
  * start date = the date of the experiment's initial deposit (ledger INITIAL row, MYT date). Entry price = the last FINAL (end-of-day) close on or before that date, i.e. a price that
    was known at the start (no look-ahead).  Weekend start -> Friday close.
  * FX: RM10,000 converted at the USD/MYR MID of the start date with the SAME configured spread as real conversions (fx_spread_bps, 30 bps) -> consistent with the AI account;
    valuation afterwards at daily mid (identical to the AI equity curve: no exit spread in either).
  * shares: FRACTIONAL (an index-fund equivalent). The real account holds WHOLE shares, so cash/exposure differ; that difference is reported, not hidden.
  * costs: modelled ETF half-spread (1 bp) on entry + the FX spread. No commission. No tax: US dividend withholding for non-US persons (typically 30%) is NOT modelled.
  * total return: cash dividends (yfinance Ticker.dividends, ex-date) are reinvested at that day's close when available; otherwise PRICE RETURN ONLY is flagged.
  * data: yfinance END-OF-DAY closes (final bars only)."""
import json, math
from datetime import datetime, timezone, timedelta, date
import numpy as np, pandas as pd
from .. import db, data, ledger, portfolio

IDS = {"SPY": "shadow_SPY", "QQQ": "shadow_QQQ"}

def _start_date():
    c = db.conn(); r = c.execute("SELECT ts FROM ledger WHERE uniq='INITIAL'").fetchone(); c.close()
    return ledger.myt_date(r["ts"]) if r else None

def _dividends(sym):
    c = db.conn()
    try: r = c.execute("SELECT ex_date, amount FROM dividends WHERE symbol=? ORDER BY ex_date", (sym,)).fetchall()
    except Exception: r = []
    c.close(); return [(x["ex_date"], x["amount"]) for x in r]

def ensure_tables():
    c = db.conn(); c.execute("CREATE TABLE IF NOT EXISTS dividends(symbol TEXT, ex_date TEXT, amount REAL, fetched_at TEXT, PRIMARY KEY(symbol,ex_date))"); c.commit(); c.close()

def fetch_dividends(provider_symbol_fn=None, now=None, symbols=("SPY", "QQQ")):
    """Stores cash dividends (free, yfinance). Failure is recorded, never fabricated."""
    ensure_tables(); now = now or datetime.now(timezone.utc); ok = {}
    for s in symbols:
        try:
            import yfinance as yf
            ser = (provider_symbol_fn(s) if provider_symbol_fn else yf.Ticker(s).dividends)
            c = db.conn()
            for ts, amt in ser.items():
                c.execute("INSERT OR REPLACE INTO dividends VALUES(?,?,?,?)", (s, str(pd.Timestamp(ts).date()), float(amt), now.isoformat(timespec="seconds")))
            c.commit(); c.close(); ok[s] = len(ser)
        except Exception as e: ok[s] = f"failed: {e}"
    return ok

def build(now=None, cfg=None, start=None):
    """Recomputes shadow_portfolios + shadow_values. Returns {pid: info}. Idempotent (derived)."""
    ensure_tables(); cfg = cfg or db.get_settings(); now = now or datetime.now(timezone.utc); start = start or _start_date()
    if not start: return {}
    out = {}; c = db.conn(); c.execute("DELETE FROM shadow_values"); c.execute("DELETE FROM shadow_portfolios")
    endd = now.astimezone(ledger.MYT).date(); spread = float(cfg["fx_spread_bps"]) / 1e4; start_myr = float(cfg.get("benchmark_start_myr", 10000.0)); hs = 1.0 / 1e4
    for sym, pid in IDS.items():
        df = data.load_bars(sym, include_partial=False)
        base = df[df.index <= pd.Timestamp(start)]
        if len(df) == 0 or len(base) == 0:
            out[pid] = {"error": f"no final {sym} bar on/before {start}"}; continue
        px0 = float(base["close"].iloc[-1]); d0 = base.index[-1].date(); fx0 = portfolio.fx_mid_on(start)
        if not fx0: out[pid] = {"error": "no FX rate at start"}; continue
        usd = start_myr / (fx0 * (1 + spread)); fill = px0 * (1 + hs); shares = usd / fill; costs_usd = shares * (fill - px0)
        fx_cost_myr = start_myr - usd * fx0
        divs = _dividends(sym); has_div = len(divs) > 0; sh = shares; drip = 0.0
        c.execute("INSERT INTO shadow_portfolios VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (pid, sym, str(start), start_myr, fx0, shares, 0.0, fill, costs_usd, fx_cost_myr,
                  "fractional shares; entry = last final close on/before start + 1bp half-spread; FX mid + spread; dividends reinvested at ex-date close" if has_div else "PRICE RETURN ONLY (no dividend data available)", now.isoformat(timespec="seconds")))
        d = start; dd = {x[0]: x[1] for x in divs}
        while d <= endd:
            ds = str(d)
            if ds in dd:
                pxd = df[df.index <= pd.Timestamp(d)]
                if len(pxd) and pd.Timestamp(d) <= df.index[-1]: add = sh * dd[ds] / float(pxd["close"].iloc[-1]); sh += add; drip += add * float(pxd["close"].iloc[-1])
            pxd = df[df.index <= pd.Timestamp(d)]
            if len(pxd):
                p = float(pxd["close"].iloc[-1]); fx = portfolio.fx_mid_on(d) or fx0; val = sh * p
                c.execute("INSERT OR REPLACE INTO shadow_values VALUES(?,?,?,?,?,?,?,?,?)", (pid, ds, p, "final close", sh, 0.0, val, fx, val * fx))
            d += timedelta(days=1)
        out[pid] = {"symbol": sym, "start_date": str(start), "entry_close_date": str(d0), "entry_price": px0, "fx_start": fx0, "shares_start": shares, "dividends_reinvested_usd": drip, "dividends_known": has_div,
                    "costs_usd": costs_usd, "fx_cost_myr": fx_cost_myr}
    c.commit(); c.close(); return out

def series(pid):
    c = db.conn(); r = [dict(x) for x in c.execute("SELECT * FROM shadow_values WHERE pid=? ORDER BY date", (pid,))]; c.close(); return r

def _stats(vals_myr, vals_usd, dates, rf=0.03):
    """vals: list of daily values. Metrics with explicit insufficiency handling."""
    s = pd.Series(vals_myr, index=pd.to_datetime(dates)); u = pd.Series(vals_usd, index=pd.to_datetime(dates))
    n = len(s); out = {"n_points": n, "value_myr": float(s.iloc[-1]), "value_usd": float(u.iloc[-1]), "total_return_pct": float((s.iloc[-1] / s.iloc[0] - 1) * 100) if n > 1 else None}
    out["usd_return_pct"] = float((u.iloc[-1] / u.iloc[0] - 1) * 100) if n > 1 else None
    out["fx_impact_pct"] = (out["total_return_pct"] - out["usd_return_pct"]) if n > 1 else None
    out["max_drawdown_pct"] = float((s / s.cummax() - 1).min() * 100) if n > 1 else None
    last = s.index[-1]
    def since(d0):
        b = s[s.index <= d0]; return float((s.iloc[-1] / b.iloc[-1] - 1) * 100) if len(b) and b.index[-1] != last else None
    out["monthly_return_pct"] = since(last - pd.Timedelta(days=30)); out["weekly_return_pct"] = since(last - pd.Timedelta(days=7))
    out["ytd_return_pct"] = since(pd.Timestamp(date(last.year - 1, 12, 31))) if s.index[0] <= pd.Timestamp(date(last.year - 1, 12, 31)) else (out["total_return_pct"] if s.index[0].year == last.year else None)
    days = (s.index[-1] - s.index[0]).days
    out["annualized_pct"] = float(((s.iloc[-1] / s.iloc[0]) ** (365.0 / days) - 1) * 100) if days >= 90 else None
    r = s.pct_change().dropna(); r = r[r.index.dayofweek < 5] if len(r) else r
    if len(r) >= 20 and r.std() > 0:
        ex = r.mean() - rf / 252; dn = np.sqrt((np.minimum(r - rf / 252, 0) ** 2).mean())
        out.update({"volatility_pct": float(r.std() * math.sqrt(252) * 100), "sharpe": float(ex / r.std() * math.sqrt(252)), "sortino": float(ex / dn * math.sqrt(252)) if dn > 0 else None})
    else: out.update({"volatility_pct": None, "sharpe": None, "sortino": None})
    out["risk_adjusted"] = (out["total_return_pct"] / abs(out["max_drawdown_pct"])) if (n > 1 and out.get("max_drawdown_pct") and out["max_drawdown_pct"] < -0.05 and days >= 30) else None
    out["insufficient_history_note"] = f"{n} daily point(s) / {days} day(s): annualized, Sharpe, Sortino, volatility need >=90 days / >=20 daily returns" if (days < 90 or len(r) < 20) else ""
    return out

def comparison(now=None, cfg=None):
    """AI account vs SPY vs QQQ (MYR/USD): metrics table + normalized series + exposure/cash differences. Underperformance is stated plainly."""
    now = now or datetime.now(timezone.utc); cfg = cfg or db.get_settings(); info = build(now, cfg)
    c = db.conn(); ai = [dict(r) for r in c.execute("SELECT * FROM equity_curve2 WHERE strategy_id='ALL' ORDER BY date")]; c.close()
    snap = portfolio.snapshot(now, cfg); fx_now = snap["fx"].get("rate") or 0
    res = {"method": {k: v for k, v in info.items()}, "rows": [], "series": {}, "note": ""}
    if not ai: return {**res, "note": "no AI equity history yet"}
    dates = [r["date"] for r in ai]; ai_myr = [r["equity_myr"] for r in ai]; ai_usd = [r["equity_usd"] for r in ai]
    # exposure/cash
    c = db.conn(); sv = {r["date"]: r for r in c.execute("SELECT * FROM equity_curve2 WHERE strategy_id='swing'")}; c.close()
    inv = [((sv[d]["positions_usd"] or 0) / sv[d]["equity_usd"]) if d in sv and sv[d]["equity_usd"] else 0.0 for d in dates]
    mine = _stats(ai_myr, ai_usd, dates); mine["name"] = "AI Trading Lab (swing, real forward paper)"; mine["time_invested_pct"] = round(float(np.mean([1 if x > 0.001 else 0 for x in inv])) * 100, 1)
    mine["cash_pct_now"] = round((1 - inv[-1]) * 100, 1); mine["exposure_pct_now"] = round(inv[-1] * 100, 1)
    mine["costs_myr"] = round(sum(s["fx_spread_paid_myr"] for s in snap["sleeves"].values()) + sum((s["fees_usd"] + s["spread_usd"] + s["slip_usd"]) * fx_now for s in snap["sleeves"].values()), 2)
    mine["key"] = "AI"; res["rows"].append(mine); res["series"]["AI"] = [{"date": d, "myr": m, "usd": u} for d, m, u in zip(dates, ai_myr, ai_usd)]
    for sym, pid in IDS.items():
        sv_ = series(pid); sv_ = [r for r in sv_ if r["date"] in set(dates)] or sv_
        if not sv_: res["rows"].append({"key": sym, "name": f"{sym} shadow", "error": (info.get(pid) or {}).get("error", "no data")}); continue
        st = _stats([r["value_myr"] for r in sv_], [r["value_usd"] for r in sv_], [r["date"] for r in sv_]); st["name"] = f"{sym} shadow (virtual RM10,000, fractional)"; st["key"] = sym
        i = info.get(pid) or {}; st["time_invested_pct"] = 100.0; st["cash_pct_now"] = 0.0; st["exposure_pct_now"] = 100.0; st["costs_myr"] = round((i.get("costs_usd", 0)) * (i.get("fx_start") or fx_now) + (i.get("fx_cost_myr") or 0), 2)
        st["dividends_known"] = i.get("dividends_known"); res["rows"].append(st); res["series"][sym] = [{"date": r["date"], "myr": r["value_myr"], "usd": r["value_usd"]} for r in sv_]
    me = res["rows"][0]; lines = []
    for r in res["rows"][1:]:
        if r.get("total_return_pct") is not None and me.get("total_return_pct") is not None:
            diff = me["total_return_pct"] - r["total_return_pct"]
            lines.append(f"AI account {'UNDERPERFORMS' if diff < 0 else 'outperforms'} {r['key']} by {abs(diff):.2f} percentage points since the common start (MYR). Exposure: AI {me['exposure_pct_now']}% invested / {me['cash_pct_now']}% cash vs {r['key']} 100% invested - a large part of any gap is the cash/exposure difference, not skill.")
    res["note"] = " ".join(lines) or "benchmark comparison needs >=2 daily points"
    res["start_value_myr"] = cfg.get("benchmark_start_myr", 10000.0); res["unit"] = "RM, all series begin at the same start date; AI at RM10,000 less FX spread already paid"
    return res
