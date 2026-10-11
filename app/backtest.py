"""Historical backtester (label: BACKTEST - hypothetical, NOT forward results).
Signals use only data up to the close of day t; orders execute at the open of t+1 (no look-ahead).
Uses the same feature/score/trigger/risk code as the live system EXCEPT fundamentals (yfinance has no point-in-time history):
fundamental factor excluded and weights renormalized. Survivorship bias: universe = CURRENT constituents/blue chips (see CAVEATS)."""
import math, json
from datetime import datetime, timezone
import numpy as np, pandas as pd
from . import config, db, scoring, risk, data, calendars as cal

CAVEATS = [
    "SURVIVORSHIP BIAS: the universe is today's blue chips (yfinance has no historical constituents). Delisted/failed names are absent, so results are biased UPWARD.",
    "Fundamentals excluded (no point-in-time data): score uses momentum/technical/RS-regime/liquidity/risk with renormalized weights.",
    "Prices are Yahoo daily bars (split-adjusted, not dividend-adjusted); dividends ignored. Data errors possible.",
    "Fills at next open +/- configured slippage and fees; real fills, queue priority, halts and limit-up/down are not modeled. Bursa lots of 100.",
    "Parameter selection used the train/validation windows only; the test window was evaluated once. With few trades, results are statistically weak.",
    "This is NOT proof the strategy works. It is a hypothetical simulation and says nothing certain about future returns.",
]

def signal_arrays(df: pd.DataFrame, bench: pd.DataFrame, regimes: pd.Series, fx_const=1.0):
    """Causal per-day signals for one symbol. Returns DataFrame indexed by date with score, trig, mv, atr, close, sma50 ..."""
    f = scoring.feature_frame(df, bench["close"] if bench is not None else None)
    rg = regimes.reindex(f.index, method="ffill").fillna("unknown")
    sc = np.full(len(f), np.nan); trig = np.zeros(len(f), bool); mvp = np.zeros(len(f), bool)
    for i in range(len(f)):
        row = f.iloc[i]
        if pd.isna(row["sma200"]) or pd.isna(row["atr14"]) or pd.isna(row["adv20_local"]): continue
        comp = scoring.composite(row, fund=None, regime=rg.iloc[i], fx=fx_const, use_fundamentals=False, light=True)
        sc[i] = comp["score"]; mvp[i] = comp["minervini"]["pass"]; trig[i] = scoring.entry_trigger(row)["fired"]
    out = f[["open", "high", "low", "close", "atr14", "sma50", "adv20_local"]].copy()
    out["score"] = sc; out["trig"] = trig; out["mv"] = mvp; out["regime"] = rg.values
    return out

def regime_series(bench: pd.DataFrame) -> pd.Series:
    f = scoring.feature_frame(bench)
    return pd.Series([scoring.classify_regime(f.iloc[i])["regime"] for i in range(len(f))], index=f.index)

def simulate(sig: dict, market_of: dict, fx: pd.Series, dates, cfg, start_capital=10000.0):
    """sig: symbol -> signal DataFrame (reindexed to master dates). Returns equity Series, trades, stats."""
    pend_ref = {}
    cash = start_capital; pos = {}; trades = []; eq = []; pend_entries = []; pend_exits = set(); costs = 0.0; exposure = []
    fxp = fx.shift(1).reindex(dates, method="ffill").bfill() if len(fx) else pd.Series(1.0, index=dates)
    weekly = bool(cfg.get("weekly_entries_only", False))
    for t_i, t in enumerate(dates):
        wk_end = (t_i + 1 >= len(dates)) or dates[t_i + 1].isocalendar()[1] != t.isocalendar()[1]
        fxv = {"BURSA": 1.0, "US": float(fxp.iloc[t_i]) if len(fxp) else 1.0}
        def px(s, col): return sig[s][col].iat[t_i]
        # 1) execute exits at open
        for s in list(pend_exits):
            if s in pos and not pd.isna(px(s, "open")):
                cash += _sell(pos, s, float(px(s, "open")), t, fxv, market_of, cfg, trades, "exit-signal"); costs += trades[-1]["fees"]
                pend_exits.discard(s)
        # 2) execute entries at open
        eqv = cash + sum(p["qty"] * (px(s, "close") if not pd.isna(px(s, "close")) else p["last"]) * fxv[market_of[s]] for s, p in pos.items())
        for s, stop in pend_entries:
            if s in pos or len(pos) >= cfg["max_positions"] or pd.isna(px(s, "open")): continue
            o = float(px(s, "open")); m = market_of[s]
            if o <= stop or o > 1.03 * pend_ref[s]: continue
            held_val = sum(p["qty"] * p["last"] * fxv[market_of[q]] for q, p in pos.items())
            sz = risk.size_position(equity=eqv, cash=cash, price=o, stop=stop, market=m, fx=fxv[m], cfg=cfg, adv_local=float(px(s, "adv20_local")), current_exposure=held_val)
            q = sz["qty"]
            if q < cfg["lot_size"][m]: continue
            fp = risk.slip_price(m, "BUY", o, cfg); val = fp * q; fee = risk.fees_local(m, "BUY", val, q, cfg) * fxv[m]
            cash -= val * fxv[m] + fee; costs += fee + abs(fp - o) * q * fxv[m]
            pos[s] = {"qty": q, "cost": val * fxv[m] + fee, "entry_px": fp, "entry_t": t, "stop": stop, "hi": fp, "last": o, "fees": fee, "slip": abs(fp - o) * q * fxv[m], "bars": 0}
        pend_entries = []
        # 3) intraday stops (stop orders rest at exchange; gap => open)
        for s in list(pos):
            lo, op = px(s, "low"), px(s, "open")
            if not pd.isna(lo) and lo <= pos[s]["stop"]:
                cash += _sell(pos, s, float(min(op, pos[s]["stop"])), t, fxv, market_of, cfg, trades, "stop" if op >= pos[s]["stop"] else "stop-gap"); costs += trades[-1]["fees"]
        # 4) mark + trailing + new signals at close
        for s, p in pos.items():
            c = px(s, "close")
            if not pd.isna(c):
                p["last"] = float(c); p["bars"] += 1; p["hi"] = max(p["hi"], float(c))
                a = px(s, "atr14")
                if not pd.isna(a): p["stop"] = max(p["stop"], p["hi"] - cfg["atr_stop_mult"] * float(a))
                if (wk_end or not weekly) and (float(c) < float(px(s, "sma50")) if not pd.isna(px(s, "sma50")) else False): pend_exits.add(s)
        eqv = cash + sum(p["qty"] * p["last"] * fxv[market_of[s]] for s, p in pos.items())
        eq.append(eqv); exposure.append((eqv - cash) / eqv if eqv else 0)
        cands = []
        for s in (sig if (wk_end or not weekly) else []):
            sc = px(s, "score")
            if pd.isna(sc) or s in pos: continue
            rg = sig[s]["regime"].iat[t_i]
            thr = cfg["buy_min_score"] if rg == "bullish" else cfg["sideways_min_score"]
            if rg in cfg["allowed_regimes"] and sc >= thr and px(s, "trig") and px(s, "mv"):
                cands.append((sc, s))
        cands.sort(reverse=True)
        pend_ref = {}
        for sc, s in cands[: max(0, cfg["max_positions"] - len(pos))]:
            c = float(px(s, "close")); a = float(px(s, "atr14")); pend_ref[s] = c
            pend_entries.append((s, c - cfg["atr_stop_mult"] * a))
    for s in list(pos):   # mark-to-market open positions as open trade (not realized)
        pass
    return pd.Series(eq, index=dates), trades, {"costs_myr": costs, "avg_exposure": float(np.mean(exposure)) if exposure else 0.0,
                                                "open_positions_end": len(pos)}

def _sell(pos, s, raw, t, fxv, market_of, cfg, trades, why):
    p = pos.pop(s); m = market_of[s]; fp = risk.slip_price(m, "SELL", raw, cfg); val = fp * p["qty"]
    fee = risk.fees_local(m, "SELL", val, p["qty"], cfg) * fxv[m]; proceeds = val * fxv[m] - fee
    trades.append({"symbol": s, "entry": str(p["entry_t"].date()), "exit": str(t.date()), "pnl": proceeds - p["cost"], "ret": proceeds / p["cost"] - 1,
                   "days": (t - p["entry_t"]).days, "fees": fee + p["fees"], "why": why})
    return proceeds

def stats(eq: pd.Series, trades, rf, extra=None):
    if len(eq) < 3: return {"note": "too short"}
    r = eq.pct_change().dropna(); yrs = max((eq.index[-1] - eq.index[0]).days / 365.25, 1e-9)
    tot = eq.iloc[-1] / eq.iloc[0] - 1; ex = r.mean() - rf / 252
    dd = np.sqrt((np.minimum(r - rf / 252, 0) ** 2).mean())
    p = np.array([t["pnl"] for t in trades]) if trades else np.array([])
    w, l = p[p > 0], p[p < 0]
    out = {"total_return_pct": tot * 100, "cagr_pct": ((1 + tot) ** (1 / yrs) - 1) * 100 if tot > -1 else -100.0,
           "max_drawdown_pct": float((eq / eq.cummax() - 1).min() * 100),
           "sharpe": float(ex / r.std() * math.sqrt(252)) if r.std() > 0 else None, "sortino": float(ex / dd * math.sqrt(252)) if dd > 0 else None,
           "n_trades": len(p), "win_rate_pct": float((p > 0).mean() * 100) if len(p) else None,
           "profit_factor": float(w.sum() / -l.sum()) if len(l) else None,
           "avg_holding_days": float(np.mean([t["days"] for t in trades])) if trades else None, "years": yrs}
    out.update(extra or {}); return out

def buy_hold(close_myr: pd.Series, rf):
    return stats(close_myr / close_myr.iloc[0] * 10000, [], rf)

def run(provider=None, years=7, cfg=None, symbols=None, progress=print):
    cfg = cfg or db.get_settings(); provider = provider or data.YFProvider(); rf = cfg["risk_free_rate_annual"]
    syms = symbols or (cfg["watchlist"]["BURSA"] + cfg["watchlist"]["US"])
    bm = {"BURSA": cfg["benchmarks"]["BURSA"], "US": cfg["benchmarks"]["US"]}
    raw = provider.daily(list(dict.fromkeys(syms + list(bm.values()) + [data.FX_SYMBOL])), period=f"{years}y")
    return run_on(raw, syms, bm, cfg, progress)

def run_on(raw, syms, bm, cfg, progress=print):
    rf = cfg["risk_free_rate_annual"]
    fx = raw[data.FX_SYMBOL]["close"] if data.FX_SYMBOL in raw else pd.Series(dtype=float)
    regs = {m: regime_series(raw[b]) for m, b in bm.items() if b in raw}
    sigs, mk, excluded = {}, {}, []
    for s in syms:
        m = cal.market_of(s)
        if s not in raw or len(raw[s]) < 300 or m not in regs: excluded.append(s); continue
        fxc = float(fx.iloc[-1]) if m == "US" and len(fx) else 1.0
        sigs[s] = signal_arrays(raw[s], raw[bm[m]], regs[m], fxc); mk[s] = m
    progress(f"signals computed for {len(sigs)} symbols; excluded {excluded}")
    dates = sorted(set().union(*[set(v.index) for v in sigs.values()]))
    dates = pd.DatetimeIndex(dates)
    first_ok = min(v["score"].first_valid_index() for v in sigs.values())
    dates = dates[dates >= first_ok]
    al = {s: v.reindex(dates) for s, v in sigs.items()}
    n = len(dates); i1, i2 = int(n * 0.5), int(n * 0.75)
    splits = {"train": (0, i1), "validation": (i1, i2), "test": (i2, n)}
    grid = [{"atr_stop_mult": a, "buy_min_score": b, "max_positions": mp} for a in (3.0, 4.0, 5.0) for b in (70, 75, 80) for mp in (3, 5)]
    def sim(window, params):
        c = {**cfg, **params}; lo, hi = window
        sub = {s: v.iloc[lo:hi] for s, v in al.items()}
        eq, tr, ex = simulate(sub, mk, fx, dates[lo:hi], c)
        return eq, tr, stats(eq, tr, rf, ex)
    table = []
    for g in grid:   # TEST window is deliberately NOT evaluated here: no peeking during parameter selection
        row = {"params": g}
        for nm in ("train", "validation"):
            _, _, st = sim(splits[nm], g); row[nm] = st
        table.append(row)
    ok = [r for r in table if (r["validation"].get("n_trades") or 0) >= 5] or table   # pre-registered: need >=5 validation trades
    best = max(ok, key=lambda r: (r["validation"].get("sharpe") or -9))
    bp = best["params"]
    eq_t, tr_t, st_t = sim(splits["test"], bp)
    # walk-forward: expanding train, 6-month (126 bar) test windows; params chosen on prior data only
    wf, chain, start = [], [], int(n * 0.4)
    pos_i = start
    while pos_i + 60 <= n:
        end = min(pos_i + 126, n)
        pick = max(grid, key=lambda g: (sim((0, pos_i), g)[2].get("sharpe") or -9))
        eq, tr, st = sim((pos_i, end), pick)
        wf.append({"test_from": str(dates[pos_i].date()), "test_to": str(dates[end - 1].date()), "chosen": pick, "stats": st}); chain.append(eq.pct_change().fillna(0))
        pos_i = end
    wf_ret = pd.concat(chain) if chain else pd.Series(dtype=float)
    wf_eq = (1 + wf_ret).cumprod() * 10000 if len(wf_ret) else pd.Series(dtype=float)
    wf_stats = stats(wf_eq, [], rf) if len(wf_eq) else {}
    # benchmarks over the test window and the whole span
    fxs = fx.reindex(dates, method="ffill")
    def bh(sym, lo, hi):
        c = raw[sym]["close"].reindex(dates, method="ffill").iloc[lo:hi]
        if sym == bm["US"] or cal.market_of(sym) == "US": c = c * fxs.iloc[lo:hi]
        return buy_hold(c.dropna(), rf)
    bench = {nm: {b: bh(b, *w) for b in bm.values() if b in raw} for nm, w in {**splits, "all": (0, n)}.items()}
    full_eq, full_tr, full_st = sim((0, n), bp)
    return {"label": f"BACKTEST {config.ALGO_VERSION} (hypothetical, historical; NOT forward paper results)", "algo_version": config.ALGO_VERSION, "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "period": f"{dates[0].date()} .. {dates[-1].date()}", "years": (dates[-1] - dates[0]).days / 365.25, "universe": list(sigs), "excluded": excluded,
            "splits": {k: [str(dates[a].date()), str(dates[b - 1].date())] for k, (a, b) in splits.items()},
            "grid_results": table, "selected_params": bp, "selection_rule": "highest validation Sharpe among combos with >=5 validation trades (test window not evaluated during selection)", "test_result": st_t, "full_period_result_selected_params": full_st,
            "walk_forward": {"windows": wf, "stitched_oos": wf_stats, "note": "params re-chosen on all data before each window"},
            "benchmarks_buy_and_hold_myr": bench, "assumptions": {"risk_free": rf, "fees": cfg["fees"], "slippage_bps": cfg["slippage_bps"], "lot": cfg["lot_size"],
                                                                   "start_capital": 10000, "after_cost": "all returns are net of fees and slippage"},
            "caveats": CAVEATS, "equity_curve_test": [[str(i.date()), float(v)] for i, v in eq_t.items()][::3]}

def save(result):
    c = db.conn(); c.execute("INSERT INTO backtests(ts,label,result) VALUES(?,?,?)", (result["generated"], result["label"], json.dumps(result, default=str))); c.commit(); c.close()
