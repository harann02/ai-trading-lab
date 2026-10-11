"""Performance analytics from the ledger-derived equity curve and closed trades."""
import math
from datetime import date
import numpy as np, pandas as pd
from . import db, ledger

def curve_df() -> pd.DataFrame:
    c = db.conn(); r = c.execute("SELECT * FROM equity_curve ORDER BY date").fetchall(); c.close()
    df = pd.DataFrame([dict(x) for x in r])
    if len(df): df.index = pd.to_datetime(df.pop("date"))
    return df

def nav_series(df: pd.DataFrame) -> pd.Series:
    """Time-weighted NAV index. Flows assumed at start of day: r_t = E_t / (E_{t-1} + F_t) - 1."""
    if len(df) == 0: return pd.Series(dtype=float)
    e = df["equity"].to_numpy(); f = df["flows"].to_numpy(); nav = [1.0]
    for i in range(1, len(e)):
        base = e[i - 1] + f[i]
        nav.append(nav[-1] * (e[i] / base if base > 0 else 1.0))
    return pd.Series(nav, index=df.index)

def xirr(flows):
    """flows: list of (date, amount) investor perspective (deposits negative, final value positive)."""
    if len(flows) < 2 or not (any(a < 0 for _, a in flows) and any(a > 0 for _, a in flows)): return None
    t0 = flows[0][0]
    f = lambda r: sum(a / (1 + r) ** ((d - t0).days / 365.0) for d, a in flows)
    lo, hi = -0.99, 100.0
    if f(lo) * f(hi) > 0: return None
    for _ in range(200):
        mid = (lo + hi) / 2
        if f(lo) * f(mid) <= 0: hi = mid
        else: lo = mid
    return (lo + hi) / 2

def max_drawdown(s: pd.Series):
    if len(s) == 0: return 0.0
    return float((s / s.cummax() - 1).min())

def trade_stats(trades):
    if not trades: return {"n_trades": 0}
    p = np.array([t["pnl_myr"] for t in trades]); r = np.array([t["ret"] for t in trades])
    w, l = p[p > 0], p[p < 0]
    hold = [(pd.Timestamp(t["exit_ts"]) - pd.Timestamp(t["entry_ts"])).total_seconds() / 86400 for t in trades]
    return {"n_trades": len(p), "win_rate": float((p > 0).mean()), "avg_win_myr": float(w.mean()) if len(w) else 0.0,
            "avg_loss_myr": float(l.mean()) if len(l) else 0.0,
            "profit_factor": float(w.sum() / -l.sum()) if len(l) else (None if not len(w) else float("inf")),
            "expectancy_myr": float(p.mean()), "expectancy_pct": float(r.mean()), "avg_holding_days": float(np.mean(hold)),
            "total_fees_myr": float(sum(t["fees_myr"] for t in trades)), "total_slippage_myr": float(sum(t["slip_myr"] for t in trades))}

def metrics(cfg=None) -> dict:
    cfg = cfg or db.get_settings(); rf = cfg["risk_free_rate_annual"]
    df = curve_df(); rws = ledger.rows(); pos, trades = ledger.replay(rws)
    assumptions = {"risk_free_rate_annual": rf, "annualization_days": 252, "vol": "stdev of daily TWR returns * sqrt(252)",
                   "sharpe": "(mean daily return - rf/252)/stdev * sqrt(252)", "sortino": "(mean daily return - rf/252)/downside_dev * sqrt(252), MAR=rf/252",
                   "twr": "chain-linked daily returns, external flows at start of day", "mwr": "XIRR on deposits/withdrawals + ending equity",
                   "note": "Few observations => ratios are statistically meaningless; shown as null when <20 daily returns."}
    out = {"assumptions": assumptions, "label": "FORWARD PAPER (simulated fills, delayed/EOD data)"}
    deposits = sum(r["cash_delta_myr"] for r in rws if r["kind"] in ("DEPOSIT", "WITHDRAW"))
    realized = sum(r["realized_pnl_myr"] or 0 for r in rws if r["kind"] == "SELL")
    fees = sum(r["fees_myr"] or 0 for r in rws); slip = sum(r["slippage_myr"] or 0 for r in rws)
    eq = ledger.equity_now(); unreal = sum(h["unrealized_pnl_myr"] for h in eq["holdings"])
    out.update({"net_contributions_myr": deposits, "equity_myr": eq["equity"], "total_pnl_myr": eq["equity"] - deposits,
                "total_return_pct": (eq["equity"] / deposits - 1) * 100 if deposits else None,
                "realized_pnl_myr": realized, "unrealized_pnl_myr": unreal, "total_fees_myr": fees, "total_slippage_myr": slip})
    buys = sum(r["gross_myr"] for r in rws if r["kind"] == "BUY"); sells = sum(r["gross_myr"] for r in rws if r["kind"] == "SELL")
    avg_eq = df["equity"].mean() if len(df) else None
    out["turnover"] = ((buys + sells) / 2 / avg_eq) if avg_eq else None
    if len(df) >= 2:
        nav = nav_series(df); out["twr_pct"] = (nav.iloc[-1] - 1) * 100; out["max_drawdown_pct"] = max_drawdown(nav) * 100
        rets = nav.pct_change().dropna(); out["n_daily_returns"] = len(rets)
        if len(rets) >= 20 and rets.std() > 0:
            out["ann_vol_pct"] = rets.std() * math.sqrt(252) * 100
            ex = rets.mean() - rf / 252
            out["sharpe"] = ex / rets.std() * math.sqrt(252)
            dd = np.sqrt((np.minimum(rets - rf / 252, 0) ** 2).mean())
            out["sortino"] = ex / dd * math.sqrt(252) if dd > 0 else None
        else:
            out.update({"ann_vol_pct": None, "sharpe": None, "sortino": None})
        fl = [(d.date(), -f) for d, f in df["flows"].items() if f != 0]
        # flows on first day are in equity already
        fl = [(d, a) for d, a in fl]; fl.append((df.index[-1].date(), float(df["equity"].iloc[-1])))
        m = xirr(fl); out["mwr_annualized_pct"] = m * 100 if m is not None else None
        out["mwr_note"] = "annualized; unreliable for periods < 1 year"
    else:
        out.update({"twr_pct": None, "max_drawdown_pct": None, "mwr_annualized_pct": None, "sharpe": None, "sortino": None, "ann_vol_pct": None,
                    "note": "insufficient history (<2 daily equity points)"})
    out.update(trade_stats(trades))
    return out

def periodic_returns(freq="M") -> list:
    df = curve_df()
    if len(df) < 2: return []
    nav = nav_series(df); g = nav.resample("ME" if freq == "M" else "W-SUN").last()
    first = pd.Series([1.0], index=[nav.index[0] - pd.Timedelta(days=1)])
    r = pd.concat([first, g]).pct_change().dropna()
    return [{"period": str(i.date()), "ret_pct": float(v * 100)} for i, v in r.items()]
