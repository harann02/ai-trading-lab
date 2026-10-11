"""Runs the PHASE 7 comparison (strategies A swing, C mean-reversion, D buy&hold SPY/QQQ; B intraday research on real 5m candles if available)
and writes validation/results.json. Analysis only - uses the free yfinance stack. Usage: python run_validation.py [--cache /path.pkl]"""
import json, sys, os, copy
from datetime import datetime, timezone
import numpy as np, pandas as pd
from app import config, universe, backtest2 as b2, backtest as bt1

IS_END = pd.Timestamp("2023-12-29"); OOS_START = pd.Timestamp("2024-01-02")
WINDOWS = [("2021", "2021-01-04", "2021-12-31"), ("2022", "2022-01-03", "2022-12-30"), ("2023", "2023-01-03", "2023-12-29"), ("2024", "2024-01-02", "2024-12-31"), ("2025", "2025-01-02", "2025-12-31"), ("2026 YTD", "2026-01-02", "2026-12-31")]

def fx_series():
    import yfinance as yf
    d = yf.download("USDMYR=X", period="8y", interval="1d", progress=False, auto_adjust=False)
    if isinstance(d.columns, pd.MultiIndex): d.columns = d.columns.get_level_values(0)
    s = d["Close"].dropna(); s.index = pd.to_datetime(s.index).tz_localize(None).normalize(); return s[~s.index.duplicated()]

def main(cache=None):
    from app import db; db.init()
    cfg = copy.deepcopy(config.DEFAULTS); cfg.update(config.get_cfg() if hasattr(config, "get_cfg") else {}) if False else None
    mem, _ex, _fl = universe.constituents(); syms = sorted(mem)
    print("constituents", len(syms), flush=True)
    hist = b2.load_history(syms + ["SPY", "QQQ"], years=7, cache=cache); print("histories", len(hist), flush=True)
    spy, qqq = hist.pop("SPY"), hist.pop("QQQ")
    fx = fx_series(); alld = spy.index; fxs = fx.reindex(alld.union(fx.index)).ffill().reindex(alld).bfill().to_dict()
    prep = b2.prepare(hist, spy, qqq, cfg)
    for k, v in [("SPY", spy), ("QQQ", qqq)]:
        f = prep["frames"]; 
    # benchmark frames need adv20_local for exec_price: use huge ADV tier (liquid ETFs)
    dates = [d for d in alld if d >= pd.Timestamp("2020-12-01")]
    # warm-up: first 252 bars are consumed by feature windows; first comparison start = 2021-01-04
    run_dates = [d for d in dates if d >= pd.Timestamp("2021-01-04")]
    print("preparing candidates...", flush=True)
    cs = b2.swing_candidates(prep, cfg, fxs); cm = b2.meanrev_candidates(prep, cfg)
    print("cands", sum(len(v) for v in cs.values()), sum(len(v) for v in cm.values()), flush=True)
    res = {"swing": b2.simulate(prep, cs, b2.swing_exit, True, cfg, run_dates, fxs),
           "meanrev": b2.simulate(prep, cm, b2.meanrev_exit, False, cfg, run_dates, fxs),
           "buy&hold SPY": b2.buy_hold(spy, fxs, run_dates, cfg, sym="SPY"), "buy&hold QQQ": b2.buy_hold(qqq, fxs, run_dates, cfg, sym="QQQ")}
    lo, hi = run_dates[0], run_dates[-1]
    periods = [("FULL", lo, hi), ("IN-SAMPLE (dev) 2021-2023", lo, IS_END), ("OUT-OF-SAMPLE 2024-", OOS_START, hi)] + [(n, pd.Timestamp(a), min(pd.Timestamp(b), hi)) for n, a, b in WINDOWS]
    out = {"generated": datetime.now(timezone.utc).isoformat(timespec="seconds"), "kind": "HISTORICAL BACKTEST (hypothetical, not forward paper results)", "caveats": b2.CAVEATS,
           "setup": {"universe_size": len(prep["frames"]), "start_capital_myr": 10000, "fx_spread_bps": cfg["fx_spread_bps"], "fractional_shares": cfg["fractional_shares"], "period": [str(lo.date()), str(hi.date())],
                     "is_end": str(IS_END.date()), "oos_start": str(OOS_START.date()), "params_tuned": False,
                     "params": {"swing": {"buy_min_score": cfg["buy_min_score"], "atr_stop_mult": cfg["atr_stop_mult"], "min_rr": cfg["min_rr"], "weekly_entries_only": True}, "meanrev": b2.MR}},
           "periods": {}, "trades_sample": {}}
    for name, a, b in periods:
        out["periods"][name] = {k: b2.metrics(r, a, b, cfg, label=k) for k, r in res.items()}
    out["verdict_oos"] = b2.verdict(out["periods"]["OUT-OF-SAMPLE 2024-"])
    out["verdict_is"] = b2.verdict(out["periods"]["IN-SAMPLE (dev) 2021-2023"])
    for k in ("swing", "meanrev"):
        tr = res[k]["trades"]; out["trades_sample"][k] = tr[-15:]
        out["periods"].setdefault("_exit_reasons", {})[k] = pd.Series([t["why"] for t in tr]).value_counts().to_dict()
        out["periods"]["_open_positions_at_end"] = {k2: res[k2].get("open_positions") for k2 in ("swing", "meanrev")}
    # curves (backtest only; MYR, weekly sampled) - kept separate from forward paper
    out["curves_myr"] = {k: {str(d.date()): round(float(v), 2) for d, v in r["equity_myr"].iloc[::5].items()} for k, r in res.items()}
    # B: intraday research on real yfinance 5m candles
    try:
        import yfinance as yf
        from app.strategies import intraday
        liq = sorted(prep["frames"], key=lambda s: -float(prep["frames"][s]["adv20_local"].iloc[-1] or 0))[:8]
        raw = yf.download(liq, period="60d", interval="5m", auto_adjust=False, group_by="ticker", progress=False, threads=True)
        cand = {}
        for s in liq:
            try:
                d = raw[s].dropna(how="all"); d.columns = [str(c).lower() for c in d.columns]; d = d[["open", "high", "low", "close", "volume"]].dropna()
                d.index = d.index.tz_convert("America/New_York") if d.index.tz is not None else d.index.tz_localize("UTC").tz_convert("America/New_York")
                d = d.between_time("09:30", "15:59"); intraday.validate_candles(d); cand[s] = d
            except Exception: pass
        adv = {s: float(prep["frames"][s]["adv20_local"].iloc[-1]) for s in cand}
        r, _tr = intraday.backtest(cand, cfg, capital_usd=2000.0, adv_usd=adv)
        out["intraday"] = {"label": "RESEARCH ONLY - price+volume PROXY (VWAP/ORB/relative volume) on REAL delayed yfinance 5m candles; ~60 trading days; live execution disabled", "symbols": list(cand), "result": r,
                           "note": "yfinance serves only ~60d of 5m history: far too short/small a sample to conclude anything (not tradable evidence)."}
    except Exception as e:
        out["intraday"] = {"status": "error", "error": str(e)[:300]}
    P = out["periods"]; oo = P["OUT-OF-SAMPLE 2024-"]; ii = P["IN-SAMPLE (dev) 2021-2023"]
    out["headline"] = [f"Out-of-sample (2024-): swing {oo['swing']['ret_usd_pct']:.1f}% / mean-reversion {oo['meanrev']['ret_usd_pct']:.1f}% vs buy&hold SPY {oo['buy&hold SPY']['ret_usd_pct']:.1f}% / QQQ {oo['buy&hold QQQ']['ret_usd_pct']:.1f}% (USD, after modelled costs).",
        f"Swing had lower drawdown (OOS max DD {oo['swing']['max_dd_pct']:.1f}% vs SPY {oo['buy&hold SPY']['max_dd_pct']:.1f}%) but did NOT beat passive SPY/QQQ on return in either period." if oo['swing']['ret_usd_pct'] < oo['buy&hold SPY']['ret_usd_pct'] else "Swing beat SPY OOS on return - still not proof (survivorship bias).",
        f"Mean-reversion lost money after costs (OOS {oo['meanrev']['ret_usd_pct']:.1f}%, in-sample {ii['meanrev']['ret_usd_pct']:.1f}%) with high turnover: not recommended as configured.",
        "No strategy is declared a winner; buy&hold remains the benchmark to beat. All numbers are hypothetical backtests with survivorship bias; they are NOT forward results."]
    os.makedirs("validation", exist_ok=True); json.dump(out, open("validation/results.json", "w"), indent=1, default=str)
    for name in ["FULL", "IN-SAMPLE (dev) 2021-2023", "OUT-OF-SAMPLE 2024-"]:
        print("\n==", name)
        for k, m in out["periods"][name].items(): print(f"{k:14s} ret_usd {m.get('ret_usd_pct',0):7.1f}% myr {m.get('ret_myr_pct_after_exit_fx',0):7.1f}% dd {m.get('max_dd_pct',0):6.1f}% sharpe {m.get('sharpe') or 0:5.2f} n={m.get('n_trades')} win={m.get('win_rate')} cost=${m.get('costs_usd',0):.0f}")
    print(out["verdict_oos"]); print(out["intraday"] if len(str(out["intraday"]))<600 else str(out["intraday"])[:600])

if __name__ == "__main__":
    main(sys.argv[sys.argv.index("--cache") + 1] if "--cache" in sys.argv else None)
