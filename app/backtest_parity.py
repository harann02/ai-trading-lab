"""Backtest that uses the SAME decision rules / config as the forward swing engine ("parity-minus-fundamentals").
METHODOLOGY_ID identifies the rule set; changing any rule means a new id (results are never mixed across ids).

What is shared with the live engine (imported, not copied): scoring.composite / minervini / entry_trigger / plan_levels, the live default config (config.DEFAULTS),
risk.size_position_usd + exec_price + fees (sizing, cash reserve, exposure, 1% risk, 20% cap, whole shares), the exit rules (stop/target/SMA50 trend break + ATR trailing stop) and the
live gating order: universe filter -> shortlist = top `universe_fundamentals_top_n` by LIGHT score among the universe -> checks (regime, score threshold, minervini, entry trigger,
cadence, reward/risk, execution quality) -> risk-sized order for the next session.
What cannot be shared (documented in docs/BACKTEST_LIVE_PARITY.md): point-in-time FUNDAMENTALS (unavailable free) -> factor removed, remaining weights renormalised proportionally
(scoring.composite does this: 25/20/20/15/10/10 -> 25/20/15/10/10 divided by 80 = x1.25), analyst targets, forward delayed 5-minute fills (daily bars only: next-session open).
Only information available at each decision date is used (features are causal: value at t uses data <= t)."""
import numpy as np, pandas as pd
from . import scoring, backtest2 as b2

METHODOLOGY_ID = "swing-parity-minus-fundamentals-1"
LEGACY_ID = "legacy-v2-swing-backtest (pre-filter, no shortlist gate, no execution-quality check, no fundamentals)"
WEIGHT_RULE = "fundamental factor (20%) removed; remaining five weights renormalised proportionally (divide by 0.80), identical to the live engine when fundamentals are unavailable for a symbol"

def swing_candidates_parity(prep, cfg, fxs):
    """date -> [(score, symbol, plan)] under the live gating order. See module docstring."""
    flt = cfg["universe_filters"]; thr_b, thr_s = cfg["buy_min_score"], cfg["sideways_min_score"]; topn = cfg["universe_fundamentals_top_n"]
    cad = cfg.get("entry_cadence", "weekly"); frames = prep["frames"]
    masks = {s: b2.universe_mask(f, flt).fillna(False) for s, f in frames.items()}
    mask_df = pd.DataFrame(masks); rs = pd.DataFrame({s: f["rs252"].where(f["rs252"].notna(), f["rs126"]) for s, f in frames.items()})
    rs_u = rs.where(mask_df); cnt = rs_u.notna().sum(axis=1); pct = rs_u.rank(axis=1, pct=True) * 100; pct[cnt < 5] = np.nan          # percentile among the FILTERED universe, as live
    all_dates = sorted(set().union(*[set(f.index) for f in frames.values()]))
    dec_dates = [t for t in all_dates if (cad != "weekly" or t.weekday() == 4)]
    cands = {}; stats = {"decision_dates": 0, "shortlisted": 0, "passed_all": 0}
    for t in dec_dates:
        reg = prep["regime"].get(t, "unknown")
        if reg not in cfg["allowed_regimes"]: continue
        pre = []
        for s, f in frames.items():
            if t not in f.index or not mask_df.at[t, s]: continue
            row = f.loc[t]
            if pd.isna(row["atr14"]) or pd.isna(row["close"]): continue
            p = pct.at[t, s] if s in pct.columns else np.nan
            comp = scoring.composite(row, fund=None, regime=reg, fx=fxs.get(t, 4.2), rs_pct=None if pd.isna(p) else float(p), use_fundamentals=False, light=True)
            pre.append((comp["score"], s, row, comp))
        if not pre: continue
        stats["decision_dates"] += 1; pre.sort(key=lambda x: -x[0])
        for score, s, row, comp in pre[:topn]:
            stats["shortlisted"] += 1
            if score < (thr_b if reg == "bullish" else thr_s): continue
            if not comp["minervini"]["pass"]: continue
            trg = scoring.entry_trigger(row)
            if not trg["fired"]: continue
            if cad == "breakout_daily" and not (trg["type"] == "breakout" and row.get("vol_ratio") is not None and not pd.isna(row.get("vol_ratio")) and row["vol_ratio"] >= 1.5): continue
            plan = scoring.plan_levels(row, cfg, None)
            if plan["rr"] is not None and plan["rr"] < cfg["min_rr"]: continue
            adv = float(row["adv20_local"]) if not pd.isna(row["adv20_local"]) else 0.0
            if not (adv >= flt["min_adv_usd"] and row["atr_pct"] <= 0.08): continue                               # live 'execution_quality' check
            cands.setdefault(t, []).append((score, s, plan)); stats["passed_all"] += 1
    return cands, stats
