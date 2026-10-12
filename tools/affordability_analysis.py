"""Offline small-account affordability analysis on a COPY of the ledger DB (read-only use; writes only a JSON/markdown to stdout/--out).
Usage: AITL_DB=/path/copy.db python tools/affordability_analysis.py [--equity 2441.85] [--out result.json]"""
import sys, os, json, argparse, collections
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pandas as pd, numpy as np
from app import db, data, scoring, risk, affordability as AFF, config

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--equity", type=float, default=2441.85); ap.add_argument("--out"); a = ap.parse_args()
    cfg = db.get_settings(); eq = a.equity; cfg_f = {**cfg, "fractional_shares": True}
    c = db.conn(); syms = [r["symbol"] for r in c.execute("SELECT symbol FROM universe_members WHERE passed=1")] if True else []
    bench = data.load_bars("SPY"); res = collections.Counter(); resf = collections.Counter(); rows = []
    for s in syms:
        df = data.load_bars(s)
        if len(df) < 260: res["INSUFFICIENT_BARS"] += 1; continue
        f = scoring.feature_frame(df, bench["close"]).iloc[-1]
        if pd.isna(f["atr14"]): res["UNAFFORDABLE_INVALID_STOP"] += 1; continue
        plan = scoring.plan_levels(f, cfg, None)
        kw = dict(equity_usd=eq, usable_cash_usd=eq * (1 - cfg["min_cash_pct"] / 100), price=plan["entry"], stop=plan["stop"], adv_usd=float(f["adv20_local"]) if not pd.isna(f.get("adv20_local")) else None, sleeve_equity_usd=eq)
        sz = risk.size_position_usd(cfg=cfg, **kw); code, det = AFF.classify(sz, 1); res[code] += 1
        sf = risk.size_position_usd(cfg=cfg_f, **kw); codef, _ = AFF.classify(sf, 0.0001); resf[codef] += 1
        rows.append({"symbol": s, "price": round(plan["entry"], 2), "atr": round(plan["atr"], 2), "stop": round(plan["stop"], 2), "code": code, "qty": sz["qty"], "frac_qty": sf["qty"], "rr": plan["rr"], "score_light": None})
    cap = eq * cfg["max_position_pct"] / 100; budget = eq * cfg["risk_per_trade_pct"] / 100
    # eligible-opportunity impact from the latest stored swing decisions (full checks exist for the shortlist)
    dec = [dict(r) for r in c.execute("SELECT symbol,decision,score,checks FROM decisions WHERE scan_date=(SELECT MAX(scan_date) FROM decisions) AND algo_version LIKE 'swing%' AND decision IN ('WATCH','BUY ELIGIBLE')")]
    imp = collections.Counter(); blockers = collections.Counter(); only_size = []
    for d in dec:
        ch = json.loads(d["checks"] or "[]"); failed = {k["name"] for k in ch if not k["pass"] and k["name"] != "automation_on"}
        for k in failed: blockers[k] += 1
        if not failed: imp["already_eligible"] += 1
        elif failed <= {"position_size>=1_share", "affordability_gate"}: imp["blocked_only_by_whole_share_size"] += 1; only_size.append(d["symbol"])
        elif failed <= {"position_size>=1_share", "affordability_gate", "reward_to_risk>=min"}: imp["blocked_by_size_and_rr"] += 1
    out = {"equity_usd": eq, "single_stock_cap_usd": round(cap, 2), "risk_budget_usd": round(budget, 2), "evaluated": sum(res.values()), "whole_share_codes": dict(res), "fractional_whatif_codes": dict(resf),
           "affordable_whole": res.get("AFFORDABLE", 0), "affordable_fractional_whatif": resf.get("AFFORDABLE", 0), "shortlist_watch_or_buy": len(dec), "shortlist_impact": dict(imp), "shortlist_blockers": dict(blockers.most_common(10)),
           "blocked_only_by_size": only_size, "sample_unaffordable": [r for r in rows if r["code"] != "AFFORDABLE"][:8], "price_quantiles": dict(zip(["p10", "p50", "p90"], [round(float(np.quantile([r['price'] for r in rows], q)), 2) for q in (.1, .5, .9)]))}
    print(json.dumps(out, indent=1)); 
    if a.out: json.dump(out, open(a.out, "w"), indent=1)

if __name__ == "__main__": main()
