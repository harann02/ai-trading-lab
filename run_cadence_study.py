"""Controlled entry-cadence study (weekly vs daily vs breakout-confirmed daily) on the same universe/params. Analysis only (HISTORICAL BACKTEST).
PRE-DECLARED design (written before running; nothing tuned on the test window):
  * dev window (IS) 2021-2023, test window (OOS) 2024-onward; parameters identical across arms and equal to the live defaults.
  * adopt DAILY (or breakout_daily) as the default ONLY IF ALL hold: OOS n_trades >= 30; OOS AND IS net return (MYR after exit FX) >= weekly; OOS Sharpe >= weekly;
    OOS max drawdown not worse than weekly by > 3 percentage points; still >= weekly under 2x costs. Otherwise weekly stays default and cadence remains a setting.
  * cost sensitivity (x1, x2, x3 modelled spread+slippage), regime analysis (calendar-year windows), parameter stability (+/- one step; reported, never used to pick).
Usage: python run_cadence_study.py [--cache /workspace/cache/hist7y.pkl]"""
import json, sys, os, copy
from datetime import datetime, timezone
import numpy as np, pandas as pd
from app import config, universe, backtest2 as b2, db
import run_validation as rv

ARMS = ["weekly", "daily", "breakout_daily"]
def main(cache):
    db.init(); cfg0 = copy.deepcopy(config.DEFAULTS)
    mem, _e, _f = universe.constituents(); syms = sorted(mem)
    hist = b2.load_history(syms + ["SPY", "QQQ"], years=7, cache=cache); spy, qqq = hist.pop("SPY"), hist.pop("QQQ")
    fx = rv.fx_series(); alld = spy.index; fxs = fx.reindex(alld.union(fx.index)).ffill().reindex(alld).bfill().to_dict()
    prep = b2.prepare(hist, spy, qqq, cfg0); run_dates = [d for d in alld if d >= pd.Timestamp("2021-01-04")]; lo, hi = run_dates[0], run_dates[-1]
    periods = {"FULL": (lo, hi), "IS 2021-2023": (lo, rv.IS_END), "OOS 2024-": (rv.OOS_START, hi)}
    out = {"generated": datetime.now(timezone.utc).isoformat(timespec="seconds"), "kind": "HISTORICAL BACKTEST (hypothetical; NOT forward evidence)", "universe_size": len(prep["frames"]),
           "decision_rule": __doc__.split("PRE-DECLARED")[1].split("Usage")[0].strip(), "arms": {}, "cost_sensitivity": {}, "parameter_stability": {}, "regime_years": {}, "limitations": b2.CAVEATS +
           ["Universe = CURRENT index members (survivorship bias): delisted/removed names are absent, which flatters every arm; see docs/VALIDATION.md for the membership check.",
            "Three arms on one historical path: differences within noise are not evidence. Daily entries multiply trades, so costs and overfitting risk rise."]}
    def run(cfg):
        cs = b2.swing_candidates(prep, cfg, fxs); r = b2.simulate(prep, cs, b2.swing_exit, True, cfg, run_dates, fxs)
        return r, {n: b2.metrics(r, a, b, cfg, label=n) for n, (a, b) in periods.items()}
    results = {}
    for arm in ARMS:
        cfg = copy.deepcopy(cfg0); cfg["entry_cadence"] = arm; print("arm", arm, flush=True); r, m = run(cfg); results[arm] = (r, m); out["arms"][arm] = m
        out["regime_years"][arm] = {n: b2.metrics(r, pd.Timestamp(a), min(pd.Timestamp(b), hi), cfg, label=n) for n, a, b in rv.WINDOWS}
    for mult in (1, 2, 3):
        cfg = copy.deepcopy(cfg0); cfg["slippage_bps"] = {**cfg["slippage_bps"], "US": cfg["slippage_bps"]["US"] * mult}; cfg["spread_model"] = {**cfg["spread_model"], "tiers": [[lo_, b * mult] for lo_, b in cfg["spread_model"]["tiers"]]}
        out["cost_sensitivity"][f"x{mult}"] = {}
        for arm in ARMS:
            cfg["entry_cadence"] = arm; print("cost", mult, arm, flush=True); r, m = run(cfg); out["cost_sensitivity"][f"x{mult}"][arm] = {k: m[k] for k in ("IS 2021-2023", "OOS 2024-")}
    for name, patch in {"buy_min_score-5": {"buy_min_score": cfg0["buy_min_score"] - 5}, "buy_min_score+5": {"buy_min_score": cfg0["buy_min_score"] + 5}, "atr_stop_mult-0.5": {"atr_stop_mult": cfg0["atr_stop_mult"] - 0.5}, "atr_stop_mult+0.5": {"atr_stop_mult": cfg0["atr_stop_mult"] + 0.5}}.items():
        out["parameter_stability"][name] = {}
        for arm in ("weekly", "daily"):
            cfg = copy.deepcopy(cfg0); cfg.update(patch); cfg["entry_cadence"] = arm; print("param", name, arm, flush=True); r, m = run(cfg); out["parameter_stability"][name][arm] = {k: m[k] for k in ("IS 2021-2023", "OOS 2024-")}
    # total-return benchmark (adjusted close incl. dividends) over the same windows, MYR with the same FX
    try:
        import yfinance as yf
        bm = {}
        for s in ("SPY", "QQQ"):
            d = yf.download(s, period="8y", interval="1d", auto_adjust=False, progress=False)
            if isinstance(d.columns, pd.MultiIndex): d.columns = d.columns.get_level_values(0)
            a = d["Adj Close"].dropna(); a.index = pd.to_datetime(a.index).tz_localize(None).normalize(); bm[s] = {}
            for n, (a0, b0) in periods.items():
                w = a[(a.index >= a0) & (a.index <= b0)]
                if len(w) > 5: bm[s][n] = {"total_return_pct_usd_adjclose": float((w.iloc[-1] / w.iloc[0] - 1) * 100), "price_only_pct_usd": float((spy if s == 'SPY' else qqq)["close"].reindex(w.index).pipe(lambda x: x.iloc[-1] / x.iloc[0] - 1) * 100)}
        out["benchmarks_total_return"] = bm
    except Exception as e: out["benchmarks_total_return"] = {"error": str(e)}
    # decision
    w, d, bo = (out["arms"][a] for a in ("weekly", "daily", "breakout_daily")); dec = {}
    for arm in ("daily", "breakout_daily"):
        a = out["arms"][arm]; c2 = out["cost_sensitivity"]["x2"]; ok = {}
        ok["oos_trades>=30"] = (a["OOS 2024-"].get("n_trades") or 0) >= 30
        ok["oos_net_return>=weekly"] = a["OOS 2024-"]["ret_myr_pct_after_exit_fx"] >= w["OOS 2024-"]["ret_myr_pct_after_exit_fx"]
        ok["is_net_return>=weekly"] = a["IS 2021-2023"]["ret_myr_pct_after_exit_fx"] >= w["IS 2021-2023"]["ret_myr_pct_after_exit_fx"]
        ok["oos_sharpe>=weekly"] = (a["OOS 2024-"].get("sharpe") or -9) >= (w["OOS 2024-"].get("sharpe") or -9)
        ok["oos_mdd_not_worse_by_3pp"] = a["OOS 2024-"]["max_dd_pct"] >= w["OOS 2024-"]["max_dd_pct"] - 3
        ok["x2_cost_oos>=weekly_x2"] = c2[arm]["OOS 2024-"]["ret_myr_pct_after_exit_fx"] >= c2["weekly"]["OOS 2024-"]["ret_myr_pct_after_exit_fx"]
        dec[arm] = {"checks": ok, "adopt": all(ok.values())}
    out["decision"] = {**dec, "default_cadence": "daily" if dec["daily"]["adopt"] else ("breakout_daily" if dec["breakout_daily"]["adopt"] else "weekly"),
                       "statement": "Adopted only if every pre-declared check passed; otherwise weekly remains the default and the cadence is an owner setting (entry_cadence)."}
    json.dump(out, open("validation/cadence_study.json", "w"), indent=1, default=str); print(json.dumps(out["decision"], indent=1))
if __name__ == "__main__":
    c = sys.argv[sys.argv.index("--cache") + 1] if "--cache" in sys.argv else None; main(c)
