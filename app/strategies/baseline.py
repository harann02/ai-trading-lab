"""Strategy D - Buy-and-hold SPY/QQQ baseline account. Own sleeve; buys equal USD weights ONCE at the next open and never trades again (no stop, no signals).
Risk-engine limits (reserve/exposure/risk-per-trade) are intentionally N/A: it is a passive benchmark that pays the same real costs (FX spread, spread/slippage, fees)."""
import json
from .. import db, ledger, engine, reports
from . import common as C

SID = "baseline"; VERSION = "baseline-2.0.0"; NAME = "Buy & hold SPY/QQQ"; REQUIRES = ["eod"]

def scan(ctx):
    cfg = ctx.cfg; res = {"counts": {k: 0 for k in C.STATUSES}, "rows": []}; syms = cfg.get("baseline_symbols", ["SPY", "QQQ"])
    s_ = ctx.snap["sleeves"].get(SID)
    held = set(ctx.held_syms(SID)); pend = set(ctx.pend_syms(SID))
    remaining = s_["buying_power_usd"] if s_ else 0.0; n_left = len([x for x in syms if x not in held and x not in pend])
    for s in syms:
        if s in held or s in pend: res["counts"]["HOLD"] += 1; res["rows"].append({"symbol": s, "status": "HOLD", "score": None}); continue
        if s not in ctx.frames and s not in ctx.status: res["counts"]["DATA BLOCKED"] += 1; res["rows"].append({"symbol": s, "status": "DATA BLOCKED"}); continue
        st = ctx.status.get(s)
        if not st or st["status"] != "OK": res["counts"]["DATA BLOCKED"] += 1; res["rows"].append({"symbol": s, "status": "DATA BLOCKED", "reason": st and st.get("reason")}); continue
        px = float(ctx.bars(s)["close"].iloc[-1]); alloc = remaining / max(1, n_left)
        from .. import risk
        fill, _, _ = risk.exec_price("BUY", px, None, cfg)
        qty = int(alloc // fill) if not cfg["fractional_shares"] else int(alloc / fill * 1e4) / 1e4
        if qty < 1 and not cfg["fractional_shares"] and remaining >= fill * 1.001: qty = 1       # whole shares: take 1 if the remaining pool affords it
        remaining -= qty * fill * 1.001; n_left -= 1
        d = {"decision": "BUY ELIGIBLE" if qty >= 1 or cfg["fractional_shares"] else "REJECT", "score": None, "band": "", "algo": VERSION, "checks": [
                {"name": "data_fresh", "pass": True, "detail": f"last bar {st['last_bar']}"}, {"name": "size>=1_share", "pass": qty >= 1 or cfg["fractional_shares"], "detail": f"qty {qty} @ ${px:.2f} (alloc ${alloc:,.2f})"},
                {"name": "strategy_enabled", "pass": cfg["strategies"][SID]["enabled"], "detail": ""}],
             "plan": {"entry": px, "stop": None, "target": None, "rr": None, "qty": qty, "est_cost_usd": qty * px, "max_loss_usd": None, "max_loss_myr": None, "fx": ctx.fx["rate"]},
             "scores": {"factors": {}, "flags": []}, "snapshot": {"bar_date": st["last_bar"], "fx": ctx.fx, "data_label": ctx.data_label}, "explanation": f"{s}: baseline initial purchase (buy & hold).\n{reports.TEMPLATE_LABEL}"}
        key = f"{VERSION}:INITIAL:{s}"; did = C.save_signal(key, ctx.now, ctx.sd, s, SID, d); d["id"] = did; res["counts"][d["decision"]] += 1
        if d["decision"] == "BUY ELIGIBLE" and ctx.trade and cfg.get("automation_on", True) and did and cfg["strategies"][SID]["enabled"]:
            oid = engine.create_order(decision_key=key, decision_id=did, symbol=s, market="US", side="BUY", qty=qty, ref_price=px, stop=None, target=None, reason="baseline initial buy-and-hold purchase",
                                      now=ctx.now, strategy_id=SID, signal_status=d["decision"], plan_json=json.dumps(d["plan"]))
            d["order_id"] = oid
        res["rows"].append({"symbol": s, "status": d["decision"], "score": None, "id": did, "order_id": d.get("order_id"), "plan": {"entry": px, "qty": qty}})
    return res
