"""Re-runs the historical validation with the parity methodology. Pre-declared splits (same as legacy: in-sample <= 2023-12-29, out-of-sample from 2024-01-02), NO tuning.
Writes validation/results.json (new; legacy preserved in validation/results_legacy_v2.json and referenced as LEGACY), validation/trades_swing_parity.json (full trade lists).
Usage: python run_validation_parity.py [--cache /path/hist.pkl]"""
import json, sys, os, copy, shutil
from datetime import datetime, timezone
import numpy as np, pandas as pd
from app import config, universe, backtest2 as b2, backtest_parity as bp
import run_validation as rv

def expectancy(trades):
    if not trades: return {}
    p = np.array([t["pnl"] for t in trades]); w = p[p > 0]; l = p[p <= 0]
    return {"n": len(p), "win_rate_pct": float((p > 0).mean() * 100), "profit_factor": float(w.sum() / -l.sum()) if len(l) and l.sum() < 0 else None, "expectancy_usd": float(p.mean()),
            "avg_win_usd": float(w.mean()) if len(w) else None, "avg_loss_usd": float(l.mean()) if len(l) else None, "avg_hold_days": float(np.mean([t["days"] for t in trades])), "total_pnl_usd": float(p.sum())}

def main(cache=None):
    from app import db; db.init()
    cfg = copy.deepcopy(config.DEFAULTS); mem, _ex, _fl = universe.constituents(); syms = sorted(mem); print("constituents", len(syms), flush=True)
    hist = b2.load_history(syms + ["SPY", "QQQ"], years=7, cache=cache); print("histories", len(hist), flush=True)
    spy, qqq = hist.pop("SPY"), hist.pop("QQQ"); fx = rv.fx_series(); alld = spy.index; fxs = fx.reindex(alld.union(fx.index)).ffill().reindex(alld).bfill().to_dict()
    prep = b2.prepare(hist, spy, qqq, cfg); run_dates = [d for d in alld if d >= pd.Timestamp("2021-01-04")]; lo, hi = run_dates[0], run_dates[-1]
    print("parity candidates...", flush=True); cp, st = bp.swing_candidates_parity(prep, cfg, fxs); print("cands", sum(len(v) for v in cp.values()), st, flush=True)
    cm = b2.meanrev_candidates(prep, cfg)
    res = {"swing [parity-minus-fundamentals]": b2.simulate(prep, cp, b2.swing_exit, True, cfg, run_dates, fxs),
           "meanrev [RESEARCH ONLY, legacy method]": b2.simulate(prep, cm, b2.meanrev_exit, False, cfg, run_dates, fxs),
           "buy&hold SPY": b2.buy_hold(spy, fxs, run_dates, cfg, sym="SPY"), "buy&hold QQQ": b2.buy_hold(qqq, fxs, run_dates, cfg, sym="QQQ")}
    KEY = "swing [parity-minus-fundamentals]"
    sens = {}
    for name, c2, start in (("actual account RM10,000 (whole shares)", cfg, 10000.0), ("RM25,000 (whole shares)", cfg, 25000.0), ("RM50,000 (whole shares)", cfg, 50000.0),
                            ("RM10,000 fractional shares ON (what-if, not enabled live)", {**cfg, "fractional_shares": True}, 10000.0),
                            ("RM10,000 + 10 bps extra slippage (delayed-fill proxy)", {**cfg, "slippage_bps": {**cfg["slippage_bps"], "US": cfg["slippage_bps"]["US"] + 10}}, 10000.0),
                            ("RM10,000 + 2x costs", {**cfg, "slippage_bps": {**cfg["slippage_bps"], "US": cfg["slippage_bps"]["US"] * 2}}, 10000.0)):
        r = res[KEY] if (start == 10000.0 and c2 is cfg) else b2.simulate(prep, cp, b2.swing_exit, True, c2, run_dates, fxs, start_myr=start)
        buyhold = {k: b2.buy_hold(v, fxs, run_dates, c2, start_myr=start, sym=k) for k, v in (("SPY", spy), ("QQQ", qqq))}
        sens[name] = {per: {"swing": b2.metrics(r, a, b, cfg), "SPY": b2.metrics(buyhold["SPY"], a, b, cfg), "QQQ": b2.metrics(buyhold["QQQ"], a, b, cfg), "trade_stats": expectancy([t for t in r["trades"] if a <= pd.Timestamp(t["exit"]) <= b])}
                      for per, (a, b) in {"FULL": (lo, hi), "IN-SAMPLE": (lo, rv.IS_END), "OUT-OF-SAMPLE": (rv.OOS_START, hi)}.items()}
        print("sens done", name, flush=True)
    periods = [("FULL", lo, hi), ("IN-SAMPLE (dev) 2021-2023", lo, rv.IS_END), ("OUT-OF-SAMPLE 2024-", rv.OOS_START, hi)] + [(n, pd.Timestamp(a), min(pd.Timestamp(b), hi)) for n, a, b in rv.WINDOWS]
    legacy = json.load(open("validation/results_legacy_v2.json")) if os.path.exists("validation/results_legacy_v2.json") else json.load(open("validation/results.json"))
    if not os.path.exists("validation/results_legacy_v2.json"):
        legacy["_label"] = "LEGACY (v2 swing backtest): NOT validated for the current live strategy (no fundamentals, different gating). Kept unchanged for audit."; json.dump(legacy, open("validation/results_legacy_v2.json", "w"), indent=1)
    out = {"generated": datetime.now(timezone.utc).isoformat(timespec="seconds"), "kind": "HISTORICAL BACKTEST (hypothetical, not forward paper results)", "methodology_id": bp.METHODOLOGY_ID, "weight_rule": bp.WEIGHT_RULE,
           "caveats": b2.CAVEATS + ["PARITY: this run uses the live engine's gating/sizing/exit code except fundamentals (not point-in-time available) and delayed 5-minute fills (daily bars: next-session open). See docs/BACKTEST_LIVE_PARITY.md.",
                                    "Forward-accumulating point-in-time fundamentals snapshots started 2026-10-12 (table fundamentals_pit); they cannot be used for past dates."],
           "setup": {"universe_size": len(prep["frames"]), "start_capital_myr": 10000, "fx_spread_bps": cfg["fx_spread_bps"], "fractional_shares": cfg["fractional_shares"], "period": [str(lo.date()), str(hi.date())], "is_end": str(rv.IS_END.date()), "oos_start": str(rv.OOS_START.date()),
                     "params_tuned": False, "predeclared_splits": True, "params": {"swing": {k: cfg[k] for k in ("buy_min_score", "sideways_min_score", "atr_stop_mult", "min_rr", "entry_cadence", "universe_fundamentals_top_n", "max_positions", "risk_per_trade_pct", "max_position_pct", "max_exposure_pct", "min_cash_pct")}, "meanrev": b2.MR}, "gating_stats": st},
           "periods": {}, "trades_sample": {}, "small_account_sensitivity": sens,
           "legacy": {"label": "LEGACY (not validated for the current live strategy)", "generated": legacy.get("generated"), "periods": {n: {"swing": (legacy["periods"].get(n) or {}).get("swing")} for n in ("FULL", "IN-SAMPLE (dev) 2021-2023", "OUT-OF-SAMPLE 2024-")}, "headline": legacy.get("headline")}}
    for name, a, b in periods:
        out["periods"][name] = {k: b2.metrics(r, a, b, cfg, label=k) for k, r in res.items()}
        out["periods"][name]["swing LEGACY (not validated for live)"] = ((legacy["periods"].get(name) or {}).get("swing")) or {}
    out["periods"]["_trade_stats"] = {per: {k: expectancy([t for t in res[k]["trades"] if a <= pd.Timestamp(t["exit"]) <= b]) for k in res if res[k]["trades"]} for per, (a, b) in {"FULL": (lo, hi), "IN-SAMPLE": (lo, rv.IS_END), "OUT-OF-SAMPLE": (rv.OOS_START, hi)}.items()}
    out["verdict_oos"] = b2.verdict({k: v for k, v in out["periods"]["OUT-OF-SAMPLE 2024-"].items() if "LEGACY" not in k}); out["verdict_is"] = b2.verdict({k: v for k, v in out["periods"]["IN-SAMPLE (dev) 2021-2023"].items() if "LEGACY" not in k})
    json.dump({k: res[k]["trades"] for k in res if res[k]["trades"]}, open("validation/trades_swing_parity.json", "w"), default=str)
    for k in res:
        if res[k]["trades"]: out["trades_sample"][k] = res[k]["trades"][-15:]
    out["curves_myr"] = {k: {str(d.date()): round(float(v), 2) for d, v in r["equity_myr"].iloc[::5].items()} for k, r in res.items()}
    P = out["periods"]; oo = P["OUT-OF-SAMPLE 2024-"]; ii = P["IN-SAMPLE (dev) 2021-2023"]; sw = oo[KEY]; sl = oo["swing LEGACY (not validated for live)"]
    out["headline"] = [f"METHODOLOGY {bp.METHODOLOGY_ID}: live gating/sizing/exits, fundamentals removed (weights renormalised), whole shares, RM10,000. Out-of-sample 2024-: parity swing {sw.get('ret_usd_pct', float('nan')):.1f}% vs SPY {oo['buy&hold SPY']['ret_usd_pct']:.1f}% / QQQ {oo['buy&hold QQQ']['ret_usd_pct']:.1f}% (USD, price return, dividends ignored).",
                       f"LEGACY swing (v2 backtest, NOT validated for the live strategy): OOS {sl.get('ret_usd_pct', float('nan')):.1f}% (the old +40.9% figure); full-period legacy {(P['FULL']['swing LEGACY (not validated for live)'] or {}).get('ret_usd_pct', float('nan')):.1f}%.",
                       "No strategy is declared a winner; one historical path with survivorship bias; NOT forward results."]
    json.dump(out, open("validation/results.json", "w"), indent=1, default=str)
    for name in ("FULL", "IN-SAMPLE (dev) 2021-2023", "OUT-OF-SAMPLE 2024-"):
        print("\n==", name)
        for k, m in out["periods"][name].items(): print(f"{k[:44]:44s} ret_usd {m.get('ret_usd_pct') or 0:7.1f}% dd {m.get('max_dd_pct') or 0:6.1f}% n {m.get('n_trades')} win {m.get('win_rate')}")
    print(json.dumps(out["periods"]["_trade_stats"], indent=1)[:2500])

if __name__ == "__main__": main(sys.argv[sys.argv.index("--cache") + 1] if "--cache" in sys.argv else None)
