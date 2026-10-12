"""Strategy C - Mean reversion (short-term oversold pullbacks in long-term uptrends). Own sleeve, own rules, same risk engine.
ENTRY (all): close > SMA200 (uptrend) AND RSI(2) <= 10 AND close < SMA5 (stretched below short mean) AND close > lower Bollinger(20,2) - 1 ATR sanity AND market regime != bearish AND liquidity filters.
STOP: entry - 2 x ATR14.  EXIT (at next open): close > SMA5 (mean reached) OR RSI(2) >= 70 OR 7 sessions held (time stop) OR stop.
TARGET shown = SMA5 if above entry. Min R:R 0.5 (high-win-rate/low-R style; parameters fixed a priori, not tuned)."""
import json
import numpy as np, pandas as pd
from .. import db, config, ledger, risk, scoring, engine, reports
from . import common as C
from ..v3 import policy

SID = "meanrev"; VERSION = "meanrev-2.0.0"; NAME = "Mean reversion"; REQUIRES = ["eod"]
PARAMS = {"rsi2_max": 10.0, "rsi2_exit": 70.0, "atr_stop_mult": 2.0, "time_stop_days": 7, "min_rr": 0.5}

def mr_score(f):
    """0-100 oversold-quality score (documented): 40% RSI2 depth, 25% stretch below SMA5 in ATR units, 20% trend premium over SMA200, 15% liquidity."""
    lin = scoring.lin
    depth = lin(10 - f["rsi2"], 0, 10); stretch = lin((f["sma5"] - f["close"]) / f["atr14"], 0, 2.5); trend = lin(f["close"] / f["sma200"] - 1, 0, 0.25)
    liq = scoring.score_liquidity(f, 1.0)
    parts = [(depth, .4), (stretch, .25), (trend, .20), (liq, .15)]
    return round(scoring.wavg(parts) or 0.0, 2)

def signal(f, regime):
    c = f["close"]
    need = ["sma200", "sma5", "rsi2", "atr14", "bb_lower"]
    if any(pd.isna(f.get(k)) for k in need): return False, "insufficient history", []
    conds = [("uptrend close>SMA200", c > f["sma200"]), (f"RSI2<={PARAMS['rsi2_max']:.0f}", f["rsi2"] <= PARAMS["rsi2_max"]),
             ("close<SMA5", c < f["sma5"]), ("regime not bearish", regime != "bearish")]
    return all(v for _, v in conds), f"RSI2 {f['rsi2']:.1f}, close {c:.2f} vs SMA5 {f['sma5']:.2f} / SMA200 {f['sma200']:.2f}", conds

def decide(s, c, ctx):
    cfg, snap = ctx.cfg, ctx.snap; f = c["f"]; checks = []
    C.chk(checks, "data_fresh", True, f"last bar {c['st']['last_bar']} ({c['st']['latency']})")
    C.chk(checks, "fx_available", ctx.fx["ok"], f"USDMYR {ctx.fx['rate']} ({ctx.fx.get('status')})")
    fired, det, conds = signal(f, ctx.regime["regime"])
    for name, ok in conds: C.chk(checks, name, ok, det)
    if not conds: C.chk(checks, "history", False, det)
    plan = None; qty = 0; sz = None; score = mr_score(f) if not any(pd.isna(f.get(k)) for k in ("rsi2", "sma5", "sma200", "atr14")) else 0.0
    if not pd.isna(f["atr14"]):
        entry = float(f["close"]); stop = entry - PARAMS["atr_stop_mult"] * float(f["atr14"]); tgt = float(f["sma5"]) if not pd.isna(f["sma5"]) and f["sma5"] > entry * 1.002 else None
        rr = ((tgt - entry) / (entry - stop)) if tgt and entry > stop else None
        plan = {"entry": entry, "stop": stop, "target": tgt, "target_source": "SMA5 (mean)", "rr": rr, "atr": float(f["atr14"])}
        C.chk(checks, "reward_to_risk>=min", rr is not None and rr >= PARAMS["min_rr"], f"R:R {rr if rr is None else round(rr, 2)} vs {PARAMS['min_rr']}")
        sz = C.size_for(cfg, snap, SID, entry, stop, f); qty = sz["qty"]
        C.chk(checks, "position_size>=1_share", qty >= (0.0001 if cfg["fractional_shares"] else 1), f"qty {qty} (binding {sz['binding']}; caps {sz['detail'].get('caps')})")
        C.common_checks(checks, cfg, snap, SID, s, qty, entry, ctx.n_pos, ctx.held_syms(SID), ctx.pend_syms(SID), cfg["entries_suspended"])
        adv = C.adv_usd(f) or 0
        C.chk(checks, "execution_quality", adv >= cfg["universe_filters"]["min_adv_usd"] and f["atr_pct"] <= 0.08, f"ADV ${adv:,.0f}, ATR% {f['atr_pct']*100:.2f}")
    hard = [k["name"] for k in checks if not k["pass"] and k["name"] != "automation_on"]
    status = "BUY ELIGIBLE" if not hard else ("WATCH" if (not pd.isna(f.get("rsi2")) and f["rsi2"] <= 25 and f["close"] > f.get("sma200", np.inf)) else "REJECT")
    plan_out = {**plan, "qty": qty, "size_detail": sz["detail"] if sz else None, "est_cost_usd": qty * plan["entry"], "fx": ctx.fx["rate"],
                "max_loss_usd": qty * (plan["entry"] - plan["stop"]), "max_loss_myr": qty * (plan["entry"] - plan["stop"]) * (ctx.fx["rate"] or 0)} if plan else None
    failed = [f"{k['name']} ({k['detail']})" for k in checks if not k["pass"] and k["name"] != "automation_on"]
    expl = f"{s}: {status}. Mean-reversion score {score:.1f}. " + ("All entry rules passed." if status == "BUY ELIGIBLE" else "Not eligible: " + "; ".join(failed) + ".") + "\n" + reports.TEMPLATE_LABEL
    return {"decision": status, "score": score, "band": scoring.band(score), "checks": checks, "plan": plan_out, "algo": VERSION, "scores": {"factors": {"mean_reversion": score}, "flags": [], "params": PARAMS},
            "snapshot": {"bar_date": c["st"]["last_bar"], "features": {k: (None if pd.isna(f.get(k)) else float(f[k])) for k in ("close", "sma5", "sma20", "sma200", "rsi2", "rsi14", "atr14", "atr_pct", "bb_lower", "vol_ratio", "adv20_local")},
                         "regime": ctx.regime, "fx": ctx.fx, "data_status": c["st"], "data_label": ctx.data_label, "sleeve": snap["sleeves"].get(SID), "provider": ctx.provider_name}, "explanation": expl}

def exit_reason(f, ps, entry_ts, now_date, df):
    cl = float(f["close"])
    if ps and cl <= ps["stop"]: return f"close {cl:.2f} <= stop {ps['stop']:.2f}"
    if not pd.isna(f["sma5"]) and cl > f["sma5"]: return f"mean reached: close {cl:.2f} > SMA5 {f['sma5']:.2f}"
    if not pd.isna(f["rsi2"]) and f["rsi2"] >= PARAMS["rsi2_exit"]: return f"RSI2 {f['rsi2']:.0f} >= {PARAMS['rsi2_exit']:.0f}"
    held = int((df.index > pd.Timestamp(entry_ts[:10])).sum())
    if held >= PARAMS["time_stop_days"]: return f"time stop: {held} sessions held >= {PARAMS['time_stop_days']}"
    return None

def scan(ctx):
    cfg = ctx.cfg; res = {"counts": {k: 0 for k in C.STATUSES}, "rows": []}; held = set(ctx.held_syms(SID)); cands = []
    for s in ctx.universe:
        if s not in ctx.frames: res["counts"]["DATA BLOCKED"] += 1; continue
        f = ctx.frames[s].iloc[-1]
        sc = mr_score(f) if not any(pd.isna(f.get(k)) for k in ("rsi2", "sma5", "sma200", "atr14")) else -1
        cands.append((sc, s, f))
    cands.sort(key=lambda x: -x[0]); n_dec = 0
    for sc, s, f in cands:
        c = {"f": f, "st": ctx.status[s], "fund": None, "comp": {"score": sc, "factors": {"mean_reversion": sc}, "flags": []}}
        if s in held:
            ps = ctx.pos_state(SID, s); ent = ps["entry_ts"] if ps else ledger.iso(ctx.now)
            r = exit_reason(f, ps, ent, ctx.sd, ctx.bars(s)); d = {"decision": "EXIT" if r else "HOLD", "score": sc, "band": "", "checks": [{"name": "exit_rule", "pass": not r, "detail": r or "no exit rule fired"}],
                 "plan": None, "algo": VERSION, "scores": {"factors": {"mean_reversion": sc}}, "snapshot": {"bar_date": c["st"]["last_bar"], "exit_reason": r}, "explanation": f"{s}: {'EXIT - ' + r if r else 'HOLD'}\n{reports.TEMPLATE_LABEL}", "exit_reason": r}
        else:
            fired = (not any(pd.isna(f.get(k)) for k in ("rsi2", "sma5", "sma200"))) and f["rsi2"] <= 25 and f["close"] > f["sma200"]
            if not fired and n_dec >= 0 and sc < 30:
                res["counts"]["REJECT"] += 1; res["rows"].append({"symbol": s, "status": "REJECT", "score": sc, "rsi2": None if pd.isna(f.get("rsi2")) else float(f["rsi2"]), "close": float(f["close"])}); continue
            d = decide(s, c, ctx)
            if d["decision"] == "BUY ELIGIBLE" and not policy.new_entries_allowed(SID):
                # v3: research-only. The signal is still computed and stored (visible in the Experiment Lab) but NEVER becomes an order / reservation.
                d["decision"] = "WATCH"; d["explanation"] = f"{s}: research signal only. {policy.RESEARCH_LABEL}. {policy.MEANREV_WHY}\n" + d.get("explanation", "")
                d["checks"].append({"name": "new_entries_enabled", "pass": False, "detail": policy.RESEARCH_LABEL})
            elif d["decision"] == "BUY ELIGIBLE": ctx.n_pos += 1; ctx.reserve(SID, d["plan"]["est_cost_usd"])
        res["counts"][d["decision"]] += 1; n_dec += 1
        key = f"{VERSION}:{ctx.sd}:{s}:{c['st'].get('last_bar')}"; did = C.save_signal(key, ctx.now, ctx.sd, s, SID, d); d["id"] = did
        if s in held: C.after_hold(ctx, SID, s, d, c, trailing=False)
        if d["decision"] == "BUY ELIGIBLE" and ctx.trade and cfg["auto_trade"] and cfg.get("automation_on", True) and did and policy.new_entries_allowed(SID):
            p = d["plan"]; oid = engine.create_order(decision_key=key, decision_id=did, symbol=s, market="US", side="BUY", qty=p["qty"], ref_price=p["entry"], stop=p["stop"], target=p["target"],
                    reason=f"meanrev scan {ctx.sd} RSI2 {f['rsi2']:.1f}", now=ctx.now, needs_approval=cfg["manual_approval"], strategy_id=SID, signal_status=d["decision"], plan_json=json.dumps(p))
            if oid: d["order_id"] = oid; db.event("INFO", "order", f"[meanrev] BUY order #{oid} pending {s} x{p['qty']}", ledger.iso(ctx.now))
        res["rows"].append({"symbol": s, "status": d["decision"], "score": d["score"], "rsi2": None if pd.isna(f.get("rsi2")) else float(f["rsi2"]), "close": float(f["close"]), "id": did, "order_id": d.get("order_id"),
                            "failed": [k["name"] for k in d["checks"] if not k["pass"]], "plan": d.get("plan") and {k: d["plan"].get(k) for k in ("entry", "stop", "target", "rr", "qty")}})
    return res
