"""v2 historical backtester (US only). LABEL: BACKTEST - hypothetical, never combined with forward paper results.
* Signals use data up to the close of day t; orders execute at the OPEN of t+1 (no look-ahead). Stops are checked on later bars' lows (fill at stop, or at open if gapped).
* Costs: modelled half-spread by ADV tier + slippage, SEC fee/FINRA TAF on sells, zero commission default, T+1 settled cash (cash-account), whole shares.
* FX: sleeve is funded by converting RM at the USD/MYR rate with the configured spread (0.30%); MYR result = USD result at end-date FX after a second spread on conversion back.
* NO parameter tuning: all strategy parameters are fixed a priori (documented); periods are consecutive out-of-sample windows (walk-forward without refit).
* CAVEATS (always disclosed): survivorship bias (current S&P500/Nasdaq-100 members), no point-in-time fundamentals (swing runs without the fundamental factor, weights renormalised),
  dividends ignored, Yahoo data errors possible, no queue/halts modelling, simple spread model."""
import json, math, os, pickle
from datetime import datetime, timezone
import numpy as np, pandas as pd
from . import config, scoring, risk, backtest as bt1

CAVEATS = [
    "SURVIVORSHIP BIAS: universe = CURRENT S&P 500 + Nasdaq-100 members (no historical constituents on the free stack). Failed/delisted names are absent, so stock-strategy results are biased UPWARD; buy&hold SPY/QQQ are unaffected.",
    "NO POINT-IN-TIME FUNDAMENTALS: yfinance exposes only current fundamentals, so the swing backtest drops the fundamental factor (20% weight) and renormalises the other five. The live swing strategy uses fundamentals when available.",
    "Prices are Yahoo split-adjusted daily bars; dividends are ignored (understates buy&hold slightly). Data errors are possible.",
    "Spread/slippage are modelled (liquidity-tiered half-spread + 5 bps slippage), not observed quotes. No market impact, halts or partial fills.",
    "Parameters are fixed a priori and were NOT tuned on these data; the 'out-of-sample' split is a pre-declared date, not a fitted one. Windows are anchored walk-forward evaluations without refitting.",
    "Small RM10,000 account with whole shares: expensive stocks are unaffordable, which shapes results (and is the real constraint).",
]

# ------------------------------------------------------------------ data
def load_history(symbols, years=7, cache=None, now=None):
    """Downloads daily bars (split-adjusted). Cached to a pickle if `cache` given (analysis only; never used for live trading)."""
    if cache and os.path.exists(cache):
        return pickle.load(open(cache, "rb"))
    import yfinance as yf
    out = {}; syms = list(dict.fromkeys(symbols))
    for i in range(0, len(syms), 80):
        chunk = syms[i:i + 80]
        raw = yf.download(chunk, period=f"{years}y", interval="1d", auto_adjust=False, group_by="ticker", progress=False, threads=True)
        for s in chunk:
            try:
                d = raw[s].dropna(how="all"); d.columns = [str(c).lower() for c in d.columns]
                d = d[["open", "high", "low", "close", "volume"]].dropna(subset=["open", "high", "low", "close"]); d.index = pd.to_datetime(d.index).tz_localize(None).normalize()
                if len(d) > 300: out[s] = d[~d.index.duplicated(keep="last")]
            except Exception: pass
    if cache: pickle.dump(out, open(cache, "wb"))
    return out

# ------------------------------------------------------------------ signal preparation (causal)
def prepare(hist, spy, qqq, cfg):
    frames = {}; rs = {}
    for s, df in hist.items():
        f = scoring.enrich(scoring.feature_frame(df, spy["close"]), df, spy["close"], qqq["close"]); frames[s] = f
        rs[s] = f["rs252"].where(f["rs252"].notna(), f["rs126"])
    rsp = pd.DataFrame(rs); pct = rsp.rank(axis=1, pct=True) * 100
    cnt = rsp.notna().sum(axis=1); pct[cnt < 5] = np.nan
    regime = bt1.regime_series(spy)
    return {"frames": frames, "rs_pct": pct, "regime": regime}

def universe_mask(f, flt):
    n = pd.Series(np.arange(1, len(f) + 1), index=f.index)
    return (f["close"] >= flt["min_price"]) & (f["adv20_local"] >= flt["min_adv_usd"]) & (n >= flt["min_history"]) & (f["vol20"].between(flt["min_vol_ann"] * 0.5, flt["max_vol_ann"]))

def swing_candidates(prep, cfg, fxs):
    """date -> list[(score, symbol, plan)] using the SAME scoring/trigger/plan code as live (fundamentals excluded)."""
    cands = {}; flt = cfg["universe_filters"]; thr_b, thr_s = cfg["buy_min_score"], cfg["sideways_min_score"]
    for s, f in prep["frames"].items():
        um = universe_mask(f, flt)
        pre = um & (f["close"] > f["sma200"]) & (f["sma50"] > f["sma150"]) & (f["close"] > f["sma50"])
        cad = cfg.get("entry_cadence") or ("weekly" if cfg.get("weekly_entries_only") else "daily")
        if cad == "weekly": pre &= pd.Series(f.index.weekday == 4, index=f.index)
        for t in f.index[pre.fillna(False).to_numpy()]:
            reg = prep["regime"].get(t, "unknown")
            if reg not in cfg["allowed_regimes"]: continue
            row = f.loc[t]
            trg = scoring.entry_trigger(row)
            if not trg["fired"]: continue
            if cad == "breakout_daily" and not (trg["type"] == "breakout" and row.get("vol_ratio") is not None and not pd.isna(row.get("vol_ratio")) and row["vol_ratio"] >= 1.5): continue
            comp = scoring.composite(row, fund=None, regime=reg, fx=fxs.get(t, 4.2), rs_pct=prep["rs_pct"].at[t, s] if not pd.isna(prep["rs_pct"].at[t, s]) else None, use_fundamentals=False, light=True)
            if not comp["minervini"]["pass"] or comp["score"] < (thr_b if reg == "bullish" else thr_s): continue
            plan = scoring.plan_levels(row, cfg, None)
            if plan["rr"] is not None and plan["rr"] < cfg["min_rr"]: continue
            cands.setdefault(t, []).append((comp["score"], s, plan))
    return cands

MR = {"rsi2_max": 10.0, "rsi2_exit": 70.0, "atr_stop_mult": 2.0, "time_stop_days": 7, "min_rr": 0.5}
def meanrev_candidates(prep, cfg):
    cands = {}; flt = cfg["universe_filters"]
    for s, f in prep["frames"].items():
        um = universe_mask(f, flt)
        m = um & (f["close"] > f["sma200"]) & (f["rsi2"] <= MR["rsi2_max"]) & (f["close"] < f["sma5"]) & f["atr14"].notna()
        for t in f.index[m.fillna(False).to_numpy()]:
            if prep["regime"].get(t, "unknown") == "bearish": continue
            row = f.loc[t]; entry = float(row["close"]); stop = entry - MR["atr_stop_mult"] * float(row["atr14"]); tgt = float(row["sma5"])
            rr = (tgt - entry) / (entry - stop) if entry > stop else None
            if rr is None or rr < MR["min_rr"]: continue
            sc = scoring.lin(10 - row["rsi2"], 0, 10) * .4 + scoring.lin((row["sma5"] - row["close"]) / row["atr14"], 0, 2.5) * .25 + scoring.lin(row["close"] / row["sma200"] - 1, 0, .25) * .2 + 15
            cands.setdefault(t, []).append((float(sc), s, {"entry": entry, "stop": stop, "target": tgt, "rr": rr, "atr": float(row["atr14"])}))
    return cands

def swing_exit(f, pos, i, cfg):
    row = f.iloc[i]
    if row["close"] <= pos["stop"]: return "stop (close)"
    if pos.get("target") and row["close"] >= pos["target"]: return "target"
    if not pd.isna(row["sma50"]) and row["close"] < row["sma50"]: return "trend break SMA50"
    return None

def meanrev_exit(f, pos, i, cfg):
    row = f.iloc[i]
    if row["close"] <= pos["stop"]: return "stop (close)"
    if row["close"] > row["sma5"]: return "mean reached (close>SMA5)"
    if row["rsi2"] >= MR["rsi2_exit"]: return "RSI2>=70"
    if i - pos["i_entry"] >= MR["time_stop_days"]: return "time stop"
    return None

# ------------------------------------------------------------------ simulator
def simulate(prep, cands, exit_fn, trailing, cfg, dates, fxs, start_myr=10000.0, fx_spread_bps=None):
    """Event loop on `dates`. USD sleeve funded via FX conversion. Returns dict(equity series (USD & MYR), trades, stats)."""
    sp = cfg["fx_spread_bps"] if fx_spread_bps is None else fx_spread_bps
    fx0 = fxs.get(dates[0], 4.2); usd0 = start_myr / (fx0 * (1 + sp / 1e4)); cash = usd0; settle = []; pos = {}; trades = []; eq = {}; pend = []; exp_days = 0
    costs = {"fees": 0.0, "spread_slip": 0.0}; frames = prep["frames"]
    for ti, t in enumerate(dates):
        # settle T+1 proceeds
        for a in [x for x in settle if x[0] <= t]: cash_settled_add = a[1]; settle.remove(a)
        settled = cash - sum(a for d, a in settle)
        # 1) exits scheduled from yesterday's close -> sell at today's open
        for s in [s for s, p in pos.items() if p.get("exit_pending")]:
            f = frames[s]
            if t not in f.index: continue
            px, spr, slp = risk.exec_price("SELL", float(f.at[t, "open"]), float(f.at[t, "adv20_local"]) if not pd.isna(f.at[t, "adv20_local"]) else None, cfg)
            _sell(pos, s, px, spr, slp, t, pos[s]["exit_pending"], cfg, trades, costs); p = None; cash += trades[-1]["net"]; settle.append((_next(dates, ti), trades[-1]["net"]))
        # 2) intraday stops (low <= stop) on held positions
        for s in list(pos):
            f = frames[s]
            if t not in f.index: continue
            p = pos[s]; lo, op = float(f.at[t, "low"]), float(f.at[t, "open"])
            if lo <= p["stop"] and ti > p["t_entry_idx"]:
                raw = min(op, p["stop"]); px, spr, slp = risk.exec_price("SELL", raw, float(f.at[t, "adv20_local"]) if not pd.isna(f.at[t, "adv20_local"]) else None, cfg)
                _sell(pos, s, px, spr, slp, t, "stop (intraday)", cfg, trades, costs); cash += trades[-1]["net"]; settle.append((_next(dates, ti), trades[-1]["net"]))
        # 3) entries scheduled from yesterday -> buy at today's open
        eqv = cash + sum(p["qty"] * _px(frames[s], t, p) for s, p in pos.items())
        for score, s, plan in pend:
            f = frames[s]
            if s in pos or len(pos) >= cfg["max_positions"] or t not in f.index: continue
            o = float(f.at[t, "open"])
            if o <= plan["stop"] or o > plan["entry"] * (1 + cfg["max_gap_chase_pct"] / 100): continue
            adv = float(f.at[t, "adv20_local"]) if not pd.isna(f.at[t, "adv20_local"]) else None
            settled = cash - sum(a for d, a in settle); held_val = sum(p["qty"] * _px(frames[q], t, p) for q, p in pos.items())
            reserve = eqv * cfg["min_cash_pct"] / 100
            sz = risk.size_position_usd(equity_usd=eqv, usable_cash_usd=max(0.0, settled - reserve), price=o, stop=plan["stop"], cfg=cfg, adv_usd=adv, sleeve_equity_usd=eqv, sleeve_exposure_usd=held_val)
            q = sz["qty"]
            if q < 1: continue
            px, spr, slp = risk.exec_price("BUY", o, adv, cfg); fee = risk.us_fee_breakdown("BUY", px * q, q, cfg)["total"]
            if px * q + fee > settled: continue
            cash -= px * q + fee; costs["fees"] += fee; costs["spread_slip"] += (spr + slp) * q
            pos[s] = {"qty": q, "ent": px, "stop": plan["stop"], "target": plan.get("target"), "t_entry": t, "t_entry_idx": ti, "i_entry": frames[s].index.get_loc(t), "cost": px * q + fee, "hi": px, "fees": fee, "sp": (spr + slp) * q}
        pend = []
        # 4) end of day: mark, trailing stop, exit signals, new candidate list for tomorrow
        mv = 0.0
        for s, p in list(pos.items()):
            f = frames[s]
            if t not in f.index: mv += p["qty"] * p.get("last", p["ent"]); continue
            i = f.index.get_loc(t); cl = float(f.at[t, "close"]); p["last"] = cl; mv += p["qty"] * cl
            if trailing and not pd.isna(f.at[t, "atr14"]):
                p["hi"] = max(p["hi"], cl); p["stop"] = max(p["stop"], p["hi"] - cfg["atr_stop_mult"] * float(f.at[t, "atr14"]))
            r = exit_fn(f, p, i, cfg)
            if r and not p.get("exit_pending"): p["exit_pending"] = r
        exp_days += 1 if mv > 0 else 0
        if t in cands and ti + 1 < len(dates):
            for c in sorted(cands[t], key=lambda x: -x[0]): pend.append(c)
        eq[t] = cash + mv
    # liquidate at last close for reporting (mark only; open positions counted in equity)
    eqs = pd.Series(eq); fx_end = fxs.get(dates[-1], fx0)
    eq_myr = eqs * pd.Series({d: fxs.get(d, fx0) for d in eqs.index})
    return {"equity_usd": eqs, "equity_myr": eq_myr, "trades": trades, "costs": costs, "usd0": usd0, "start_myr": start_myr, "fx_end": fx_end, "open_positions": len(pos), "exposure_days_pct": exp_days / len(dates) * 100}

def _next(dates, ti): return dates[ti + 1] if ti + 1 < len(dates) else dates[ti] + pd.Timedelta(days=1)
def _px(f, t, p): return float(f.at[t, "close"]) if t in f.index and not pd.isna(f.at[t, "close"]) else p.get("last", p["ent"])
def _sell(pos, s, px, spr, slp, t, why, cfg, trades, costs):
    p = pos.pop(s); val = px * p["qty"]; fee = risk.us_fee_breakdown("SELL", val, p["qty"], cfg)["total"]; net = val - fee
    trades.append({"symbol": s, "entry": str(p["t_entry"].date()), "exit": str(t.date()), "qty": p["qty"], "ent_px": p["ent"], "exit_px": px, "pnl": net - p["cost"], "ret": (net - p["cost"]) / p["cost"], "net": net,
                   "days": (t - p["t_entry"]).days, "why": why, "fees": fee + p["fees"], "spread_slip": (spr + slp) * p["qty"] + p["sp"]})
    costs["fees"] += fee; costs["spread_slip"] += (spr + slp) * p["qty"]

def buy_hold(df, fxs, dates, cfg, start_myr=10000.0, sym="SPY"):
    """Passive baseline in the same conditions: FX spread in, whole shares, one buy at first open, spread/slippage, no exit."""
    sp = cfg["fx_spread_bps"]; fx0 = fxs.get(dates[0], 4.2); usd0 = start_myr / (fx0 * (1 + sp / 1e4))
    o = float(df.at[dates[0], "open"]) if dates[0] in df.index else float(df["close"].iloc[0]); px, _, _ = risk.exec_price("BUY", o, 1e12, cfg); q = math.floor(usd0 / px)
    cash = usd0 - q * px; eq = {t: cash + q * float(df.loc[:t, "close"].iloc[-1]) for t in dates if t in df.index}
    e = pd.Series(eq); return {"equity_usd": e, "equity_myr": e * pd.Series({d: fxs.get(d, fx0) for d in e.index}), "trades": [], "costs": {"fees": 0.0, "spread_slip": (px - o) * q}, "usd0": usd0, "start_myr": start_myr, "fx_end": fxs.get(dates[-1], fx0), "qty": q}

# ------------------------------------------------------------------ metrics
def metrics(res, lo, hi, cfg, label=""):
    """Metrics for window [lo,hi] using equity inside the window (re-based at window start). MYR figures include the exit FX spread."""
    e = res["equity_usd"]; e = e[(e.index >= lo) & (e.index <= hi)]
    if len(e) < 5: return {"label": label, "note": "window too short"}
    em = res["equity_myr"].reindex(e.index); sp = cfg["fx_spread_bps"] / 1e4
    r = e.pct_change().dropna(); rf = cfg["risk_free_rate_annual"] / 252; yrs = max((e.index[-1] - e.index[0]).days / 365.25, 1e-9)
    tr = [t for t in res["trades"] if lo <= pd.Timestamp(t["exit"]) <= hi]; pnl = np.array([t["pnl"] for t in tr]) if tr else np.array([])
    out = {"label": label, "start": str(e.index[0].date()), "end": str(e.index[-1].date()), "years": round(yrs, 2), "ret_usd_pct": float((e.iloc[-1] / e.iloc[0] - 1) * 100),
           "ret_myr_pct_after_exit_fx": float((em.iloc[-1] * (1 - sp) / em.iloc[0] - 1) * 100), "cagr_usd_pct": float(((e.iloc[-1] / e.iloc[0]) ** (1 / yrs) - 1) * 100) if yrs >= 1 else None,
           "max_dd_pct": float((e / e.cummax() - 1).min() * 100), "sharpe": float((r.mean() - rf) / r.std() * math.sqrt(252)) if r.std() > 0 else None,
           "sortino": float((r.mean() - rf) / np.sqrt((np.minimum(r - rf, 0) ** 2).mean()) * math.sqrt(252)) if (np.minimum(r - rf, 0) ** 2).mean() > 0 else None,
           "n_trades": len(tr), "win_rate": float((pnl > 0).mean() * 100) if len(pnl) else None, "avg_hold_days": float(np.mean([t["days"] for t in tr])) if tr else None,
           "costs_usd": float(sum(t["fees"] + t["spread_slip"] for t in tr)) if tr else 0.0}
    return out

def verdict(rows_oos: dict, min_trades=30, min_years=2.0):
    """No winner without evidence: needs >= min_trades per active strategy AND >= min_years OOS; compares only against the buy&hold baseline."""
    msgs = []; ok = True
    for k, m in rows_oos.items():
        if k.startswith("buy&hold"): continue
        if (m.get("n_trades") or 0) < min_trades: ok = False; msgs.append(f"{k}: only {m.get('n_trades')} OOS trades (<{min_trades}) - insufficient sample")
        if (m.get("years") or 0) < min_years: ok = False; msgs.append(f"{k}: OOS window {m.get('years')}y (<{min_years}y)")
    return {"winner_declared": False, "evidence_adequate": ok, "reasons": msgs or ["sample size/time adequate by the minimum rules, but this is still one historical path with survivorship bias: no winner is declared automatically"]}
