"""Strategy A - Momentum & Swing (v2 upgrade of the v1.1 engine). Scoring math = app/scoring.py (weights 25/20/20/15/10/10, validated).
Signal statuses: BUY ELIGIBLE | WATCH | HOLD | EXIT | REJECT | DATA BLOCKED. A score never buys: every check must pass. See docs/STRATEGIES.md."""
import json
import numpy as np, pandas as pd
from .. import db, config, data, ledger, risk, scoring, calendars as cal, engine, reports
from . import common as C
from .. import affordability as AFF

SID = "swing"; VERSION = "swing-2.0.0"; NAME = "Momentum & Swing"
REQUIRES = ["eod"]

def decide(s, c, ctx):
    cfg, snap = ctx.cfg, ctx.snap
    f, comp, fund = c["f"], c["comp"], c["fund"]; score = comp["score"]; checks = []
    C.chk(checks, "data_fresh", True, f"last bar {c['st']['last_bar']} ({c['st']['latency']})")
    C.chk(checks, "fx_available", ctx.fx["ok"], f"USDMYR {ctx.fx['rate']} as of {ctx.fx['rate_ts']} ({ctx.fx.get('status')})")
    C.chk(checks, "regime_allows_entries", ctx.regime["regime"] in cfg["allowed_regimes"], f"US regime {ctx.regime['regime']}: {ctx.regime.get('reason')}")
    thr = cfg["buy_min_score"] if ctx.regime["regime"] == "bullish" else cfg["sideways_min_score"]
    C.chk(checks, "score_threshold", score >= thr, f"score {score} vs required {thr} (regime {ctx.regime['regime']})")
    C.chk(checks, "minervini_trend_template", comp["minervini"]["pass"], f"{comp['minervini']['passed']}/8" + (f", unknown: {comp['minervini']['unknown']}" if comp["minervini"]["unknown"] else ""))
    trig = scoring.entry_trigger(f)
    pb = bool(f.get("pullback")) if not pd.isna(f.get("pullback", np.nan)) else False
    C.chk(checks, "entry_trigger", trig["fired"], trig["reason"] + (" | pullback structure present (informational)" if pb else ""))
    plan = scoring.plan_levels(f, cfg, (fund or {}).get("analyst_target")) if not pd.isna(f["atr14"]) else None
    cad = cfg.get("entry_cadence", "weekly")
    if cad == "weekly":
        C.chk(checks, "weekly_swing_window", pd.Timestamp(ctx.sd).weekday() == 4, f"session {ctx.sd} ({pd.Timestamp(ctx.sd).day_name()}); entry_cadence=weekly: new entries only on the week's closing session (Fri)")
    elif cad == "breakout_daily":
        vr = f.get("vol_ratio"); okb = bool(trig["fired"] and trig["type"] == "breakout" and vr is not None and not pd.isna(vr) and vr >= 1.5)
        C.chk(checks, "breakout_confirmation", okb, f"entry_cadence=breakout_daily: needs a 20d-high breakout with volume >= 1.5x average (trigger {trig['type']}, vol ratio {None if vr is None or pd.isna(vr) else round(float(vr),2)})")
    else:
        C.chk(checks, "daily_entry_window", True, "entry_cadence=daily: entries may be evaluated every session (valid signals only; cash remains a valid decision)")
    qty, sz = 0, None
    if plan:
        if plan["rr"] is not None: C.chk(checks, "reward_to_risk>=min", plan["rr"] >= cfg["min_rr"], f"R:R {plan['rr']:.2f} vs {cfg['min_rr']} (target {plan['target']:.2f} from {plan['target_source']})")
        else: C.chk(checks, "reward_to_risk>=min", True, "no credible target exists: not invented; exit relies on ATR trailing stop")
        sz = C.size_for(cfg, snap, SID, plan["entry"], plan["stop"], f); qty = sz["qty"]
        minq = 0.0001 if cfg["fractional_shares"] else 1
        C.chk(checks, "position_size>=1_share", qty >= minq, f"qty {qty} (binding cap: {sz['binding']}; caps {sz['detail'].get('caps')})")
        acode, adet = AFF.classify(sz, minq); cap_usd = (snap["equity_usd"] or 0) * cfg["max_position_pct"] / 100
        C.chk(checks, "affordability_gate", acode == "AFFORDABLE", AFF.explain(acode, adet, cap_usd))      # small-account gate with a reason code (never relaxes a limit)
        C.common_checks(checks, cfg, snap, SID, s, qty, plan["entry"], ctx.n_pos, ctx.held_syms(SID), ctx.pend_syms(SID), cfg["entries_suspended"])
        adv = C.adv_usd(f) or 0
        C.chk(checks, "execution_quality", adv >= cfg["universe_filters"]["min_adv_usd"] and f["atr_pct"] <= 0.08, f"ADV ${adv:,.0f} (min ${cfg['universe_filters']['min_adv_usd']:,.0f}), ATR% {f['atr_pct']*100:.2f} (max 8)")
    else: C.chk(checks, "plan_available", False, "ATR unavailable")
    hard = [k["name"] for k in checks if not k["pass"] and k["name"] != "automation_on"]
    status = "BUY ELIGIBLE" if (not hard and score >= 70) else ("WATCH" if score >= 70 else "REJECT")
    plan_out = {**plan, "qty": qty, "size_detail": sz["detail"] if sz else None, "est_cost_usd": qty * plan["entry"], "fx": ctx.fx["rate"],
                "max_loss_usd": qty * (plan["entry"] - plan["stop"]), "max_loss_myr": qty * (plan["entry"] - plan["stop"]) * (ctx.fx["rate"] or 0)} if plan else None
    failed = [f"{k['name']} ({k['detail']})" for k in checks if not k["pass"] and k["name"] != "automation_on"]
    expl = f"{s}: {status}. Score {score:.1f} ({', '.join(f'{k} {v}' for k, v in comp['factors'].items())})." + ("" if status == "BUY ELIGIBLE" else (" Not eligible because: " + "; ".join(failed) + ".") if failed else "") + "\n" + reports.TEMPLATE_LABEL
    ex = {"rs_vs_spy_126": _f(f.get("rs_spy126")), "rs_vs_qqq_126": _f(f.get("rs_qqq126")), "prox_52w_high": _f(f.get("prox52")), "sma20": _f(f.get("sma20")), "ema12": _f(f.get("ema12")), "ema26": _f(f.get("ema26")),
          "structure": "breakout" if trig["type"] == "breakout" else ("pullback" if pb else ("near-high" if trig["type"] else "none"))}
    snapf = {k: (None if pd.isna(f.get(k)) else float(f[k])) for k in engine.SNAP_KEYS if k in f.index}
    snap_out = {"bar_date": c["st"]["last_bar"], "features": snapf, "extra": ex, "fundamentals": fund, "fx": ctx.fx, "regime": ctx.regime, "data_status": c["st"], "data_label": ctx.data_label,
                "sleeve": snap["sleeves"].get(SID), "provider": ctx.provider_name}
    return {"decision": status, "score": score, "band": scoring.band(score), "checks": checks, "plan": plan_out, "algo": VERSION,
            "scores": {"factors": comp["factors"], "weights": scoring.W, "weights_used": comp["weights_used"], "flags": comp["flags"], "minervini": comp["minervini"],
                       "canslim": comp["canslim"], "graham": comp["graham"]}, "snapshot": snap_out, "explanation": expl}

def _f(x): return None if x is None or (isinstance(x, float) and np.isnan(x)) else float(x)

def _affordable_universe(ctx, cands):
    """Daily 'affordable universe' count: every evaluated candidate is sized with the REAL account (whole shares) and, informationally, with fractional shares ON (a what-if; fractional stays OFF)."""
    cfg = ctx.cfg; snap = ctx.snap; eq = snap["equity_usd"] or 0; cfg_frac = {**cfg, "fractional_shares": True}; codes = []; frac_ok = 0
    for score, s, c in cands:
        f = c["f"]
        if pd.isna(f.get("atr14", np.nan)): codes.append("UNAFFORDABLE_INVALID_STOP"); continue
        plan = scoring.plan_levels(f, cfg, None)
        if not plan: codes.append("UNAFFORDABLE_INVALID_STOP"); continue
        sz = C.size_for(cfg, snap, SID, plan["entry"], plan["stop"], f); code, _ = AFF.classify(sz, 1); codes.append(code)
        if code != "AFFORDABLE" and C.size_for(cfg_frac, snap, SID, plan["entry"], plan["stop"], f)["qty"] >= 0.0001: frac_ok += 1
        elif code == "AFFORDABLE": frac_ok += 1
    AFF.record_daily(ctx.sd, SID, eq, eq * cfg["max_position_pct"] / 100, eq * cfg["risk_per_trade_pct"] / 100, codes, frac_ok, ctx.now)

def scan(ctx):
    """Rank whole universe, fundamentals for the top-N only, persist decisions for held + eligible + top-K, create pending orders for BUY ELIGIBLE (best first)."""
    cfg = ctx.cfg; res = {"counts": {k: 0 for k in C.STATUSES}, "rows": []}
    pre = []
    for s in ctx.universe:
        if s not in ctx.frames:
            res["counts"]["DATA BLOCKED"] += 1; pre.append((-1, s, None)); continue
        f = ctx.frames[s].iloc[-1]
        comp = scoring.composite(f, fund=None, regime=ctx.regime["regime"], fx=ctx.fx["rate"] or 1.0, rs_pct=ctx.pct.get(s), light=True)
        pre.append((comp["score"], s, f))
    pre.sort(key=lambda x: -x[0]); topn = {s for _, s, f in pre[: cfg["universe_fundamentals_top_n"]] if f is not None}
    held = set(ctx.held_syms(SID)); keep_full = topn | held
    cands = []
    for sc0, s, f in pre:
        if f is None: continue
        fund = ctx.fundamentals(s) if s in keep_full else None
        comp = scoring.composite(f, fund=fund, regime=ctx.regime["regime"], fx=ctx.fx["rate"] or 1.0, rs_pct=ctx.pct.get(s), use_fundamentals=fund is not None, light=s not in keep_full)
        if fund is None:
            comp["flags"].append("fundamentals not fetched for this symbol (outside top-N shortlist): weights renormalized")
        cands.append((comp["score"], s, {"f": f, "fund": fund, "comp": comp, "st": ctx.status[s]}))
    cands.sort(key=lambda x: -x[0]); saved = 0
    try: _affordable_universe(ctx, cands)
    except Exception as e: db.event("WARN", "affordability", f"affordable-universe count failed: {e}")
    for score, s, c in cands:
        if s in held:
            d = hold_or_exit(s, c, ctx)
        elif c["comp"]["canslim"] is None:                       # outside shortlist: compact row only (no heavy decision snapshot)
            d = {"decision": "REJECT" if score < 70 else "WATCH", "score": score, "compact": True,
                 "checks": [], "plan": None, "snapshot": {"bar_date": c["st"]["last_bar"]}}
            res["counts"][d["decision"]] += 1; res["rows"].append(_row(s, d, c)); continue
        else:
            d = decide(s, c, ctx)
            if d["decision"] == "BUY ELIGIBLE": ctx.n_pos += 1; ctx.reserve(SID, d["plan"]["est_cost_usd"])
        res["counts"][d["decision"]] += 1
        key = f"{VERSION}:{ctx.sd}:{s}:{c['st'].get('last_bar')}"
        did = C.save_signal(key, ctx.now, ctx.sd, s, SID, d); saved += 1; d["id"] = did
        if s in held: C.after_hold(ctx, SID, s, d, c, trailing=True)
        if d["decision"] == "BUY ELIGIBLE" and ctx.trade and cfg["auto_trade"] and cfg.get("automation_on", True) and did:
            p = d["plan"]
            oid = engine.create_order(decision_key=key, decision_id=did, symbol=s, market="US", side="BUY", qty=p["qty"], ref_price=p["entry"], stop=p["stop"], target=p["target"],
                                      reason=f"swing scan {ctx.sd} score {d['score']}", now=ctx.now, needs_approval=cfg["manual_approval"], strategy_id=SID, signal_status=d["decision"], plan_json=json.dumps(p))
            if oid:
                d["order_id"] = oid
                db.event("INFO", "order", f"[swing] BUY order #{oid} pending {s} x{p['qty']} (ref ${p['entry']:.2f}, stop ${p['stop']:.2f}){' - AWAITING MANUAL APPROVAL' if cfg['manual_approval'] else ''}", ledger.iso(ctx.now))
        res["rows"].append(_row(s, d, c))
    for sc0, s, f in pre:
        if f is None: res["rows"].append({"symbol": s, "status": "DATA BLOCKED", "score": None, "reason": ctx.status[s]["status"] if s in ctx.status else "no data"}) if False else None
    res["decisions_saved"] = saved
    return res

def _row(s, d, c):
    f = c["f"]
    return {"symbol": s, "status": d["decision"], "score": d["score"], "factors": c["comp"]["factors"], "close": _f(f["close"]), "rsi14": _f(f["rsi14"]), "r126": _f(f["r126"]),
            "prox52": _f(f.get("prox52")), "rs_spy126": _f(f.get("rs_spy126")), "rs_qqq126": _f(f.get("rs_qqq126")), "vol_ratio": _f(f["vol_ratio"]), "adv_usd": _f(f["adv20_local"]),
            "failed": [k["name"] for k in d["checks"] if not k["pass"]], "id": d.get("id"), "order_id": d.get("order_id"), "plan": d.get("plan") and {k: d["plan"].get(k) for k in ("entry", "stop", "target", "rr", "qty")}}

def hold_or_exit(s, c, ctx):
    """Held position: HOLD unless exit rule fires (stop/target/SMA50 trend break) -> EXIT (sell order at next open)."""
    f = c["f"]; df = ctx.bars(s); ps = ctx.pos_state(SID, s); reason = None
    if ps:
        cl = float(df["close"].iloc[-1])
        if cl <= ps["stop"]: reason = f"close {cl:.2f} <= stop {ps['stop']:.2f}"
        elif ps["target"] and cl >= ps["target"]: reason = f"close >= target {ps['target']:.2f}"
        elif not pd.isna(f["sma50"]) and f["close"] < f["sma50"]: reason = f"trend break: close {f['close']:.2f} < SMA50 {f['sma50']:.2f}"
    status = "EXIT" if reason else "HOLD"
    return {"decision": status, "score": c["comp"]["score"], "band": scoring.band(c["comp"]["score"]), "checks": [{"name": "exit_rule", "pass": not reason, "detail": reason or "no exit rule fired"}],
            "plan": None, "algo": VERSION, "scores": {"factors": c["comp"]["factors"], "flags": c["comp"]["flags"]}, "snapshot": {"bar_date": c["st"]["last_bar"], "exit_reason": reason},
            "explanation": f"{s}: {status}. {reason or 'holding; trailing stop ratchets with ATR'}\n{reports.TEMPLATE_LABEL}", "exit_reason": reason}
