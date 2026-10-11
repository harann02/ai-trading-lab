"""Deterministic daily/weekly reports, decision explanations and investment memos.
No LLM is used. All prose is TEMPLATE-GENERATED from computed numbers and labeled as such; text can never alter a rule outcome."""
import json, hashlib
from datetime import datetime, timezone, timedelta, date
from . import db, ledger, config

TEMPLATE_LABEL = "[TEMPLATE-GENERATED EXPLANATION - no LLM used. Rules/checks are the only decision authority.]"

def explain_decision(sym, decision, score, comp, checks, plan, hard):
    f = comp["factors"]; fs = ", ".join(f"{k} {v if v is not None else 'n/a'}" for k, v in f.items())
    lines = [f"{sym}: {decision}. Composite score {score:.1f} ({fs})."]
    if decision == "BUY" and plan:
        lines.append(f"All checks passed. Plan: entry ~{plan['entry']:.3f} (reference = last close), ATR stop {plan['stop']:.3f}, "
                     + (f"target {plan['target']:.3f} ({plan['target_source']}), R:R {plan['rr']:.2f}" if plan.get("rr") else "no credible target (trailing-stop exit)")
                     + f", size {plan['qty']} shares (~RM{plan['est_cost_myr']:.2f}).")
    else:
        failed = [f"{k['name']} ({k['detail']})" for k in checks if not k["pass"] and k["name"] != "auto_trade_enabled"]
        if failed: lines.append("Not a BUY because: " + "; ".join(failed) + ".")
    if comp["flags"]: lines.append("Flags: " + "; ".join(comp["flags"]) + ".")
    return " ".join(lines) + "\n" + TEMPLATE_LABEL

def make_memo(decision_id, sym, d, now):
    comp, plan, snap = d["scores"], d["plan"], d["snapshot"]; f = snap["features"]; fu = snap.get("fundamentals") or {}
    L = [f"INVESTMENT MEMO - {sym} - {ledger.iso(now)} - algo {config.ALGO_VERSION}", TEMPLATE_LABEL, "",
         f"Data: {snap['data_label']} Bar date {snap['bar_date']}. Paper trade only.", "",
         "THESIS (numbers from real calculations)",
         f"- Composite score {d['score']:.1f} ({d['band']}); factors {comp['factors']}.",
         f"- Minervini trend template {comp['minervini']['passed']}/8; CAN SLIM-lite {comp['canslim']['passed']}/{comp['canslim']['evaluated']} evaluated; Graham-style {comp['graham']['passed']}/{comp['graham']['evaluated']} evaluated.",
         f"- Close {f.get('close'):.3f}, SMA50 {f.get('sma50') or float('nan'):.3f}, SMA200 {f.get('sma200') or float('nan'):.3f}, RSI14 {f.get('rsi14') or float('nan'):.1f}, 126d return {(f.get('r126') or 0)*100:.1f}%, 126d excess vs benchmark {(f.get('rs126') or 0)*100:.1f}%.",
         f"- Regime: {snap['regime'].get('regime')} ({snap['regime'].get('reason')}).", "",
         "TRADE PLAN",
         f"- Entry ref {plan['entry']:.3f}, stop {plan['stop']:.3f} (ATR14 {plan['atr']:.3f} x {db.get_settings()['atr_stop_mult']}), qty {plan['qty']}, est cost RM{plan['est_cost_myr']:.2f}.",
         f"- Target: {plan['target']:.3f} via {plan['target_source']}, R:R {plan['rr']:.2f}." if plan.get("rr") else "- Target: none credible (not invented). Exit via trailing ATR stop / trend break.", ""]
    ctr = []
    if (f.get("rsi14") or 0) > 70: ctr.append(f"RSI {f['rsi14']:.0f} is overbought; pullback risk.")
    if fu.get("pe") and fu["pe"] > 30: ctr.append(f"Trailing P/E {fu['pe']:.1f} is rich versus Graham-style P/E<=15.")
    if not fu or all(v is None for k, v in fu.items() if k in ("roe", "pe", "profit_margin")): ctr.append("Fundamental data missing: fundamental factor is a neutral-with-penalty placeholder.")
    if snap["regime"].get("regime") != "bullish": ctr.append(f"Market regime is {snap['regime'].get('regime')}, not bullish.")
    if (f.get("atr_pct") or 0) > 0.04: ctr.append(f"ATR is {f['atr_pct']*100:.1f}% of price: wide stop, high volatility.")
    if (f.get("dd_from_hi") or 0) > -0.01: ctr.append("Price is at its 52-week high; breakouts at highs fail often.")
    for k, v in comp["canslim"]["criteria"].items():
        if v is False: ctr.append(f"CAN SLIM-lite not met: {k}.")
    for k, v in comp["graham"]["criteria"].items():
        if v is False: ctr.append(f"Graham-style not met: {k}.")
    L += ["COUNTERARGUMENTS"] + [f"- {x}" for x in (ctr or ["- none flagged by rules"])] + ["", "WHAT WOULD MAKE THIS FAIL",
         f"- Close below the stop {plan['stop']:.3f} (loss ~{(plan['entry']-plan['stop'])/plan['entry']*100:.1f}% before slippage/fees) or a gap through it (fills at the open, worse than stop).",
         "- Market regime turning bearish/volatile; benchmark below SMA200.", "- Data errors: yfinance is delayed/EOD and may be wrong or adjusted retroactively.",
         "- Fees/slippage drag; thin liquidity relative to order size.", "- Model risk: this score is a heuristic, not proven to have edge."]
    text = "\n".join(L)
    c = db.conn(); cur = c.execute("INSERT INTO memos(decision_id,symbol,ts,text) VALUES(?,?,?,?)", (decision_id, sym, ledger.iso(now), text)); c.commit(); mid = cur.lastrowid; c.close()
    return mid

def _myt(d): return d if isinstance(d, date) else d.astimezone(ledger.MYT).date()

def daily_facts(d: date) -> dict:
    allr = ledger.rows(); st = ledger.state_asof(d, allr)
    prev = ledger.state_asof(d - timedelta(days=1), allr) if allr and ledger.myt_date(allr[0]["ts"]) < d else None
    day_pnl = (st["equity"] - prev["equity"] - st["flows"]) if prev else 0.0
    end_ts = datetime(d.year, d.month, d.day, 23, 59, 59, tzinfo=ledger.MYT).astimezone(timezone.utc).isoformat(timespec="seconds")
    c = db.conn()
    sd = c.execute("SELECT MAX(scan_date) m FROM decisions WHERE ts<=?", (end_ts,)).fetchone()["m"]
    decs = [dict(r) for r in c.execute("SELECT symbol,decision,score,band FROM decisions WHERE scan_date=? AND ts<=? AND data_mode='LIVE' ORDER BY COALESCE(score,-1) DESC, symbol", (sd, end_ts))] if sd else []
    orders = [dict(r) for r in c.execute("SELECT id,symbol,side,qty,status,reason FROM orders WHERE created_ts<=? AND substr(created_ts,1,10)>=? ORDER BY id", (end_ts, str(d - timedelta(days=1))))]
    c.close()
    trades = [{"kind": t["kind"], "symbol": t["symbol"], "qty": t["qty"], "price_local": round(t["price_local"], 4), "fees_myr": round(t["fees_myr"], 4),
               "realized_pnl_myr": None if t["realized_pnl_myr"] is None else round(t["realized_pnl_myr"], 4)} for t in st["trades"]]
    realized_cum = sum(r["realized_pnl_myr"] or 0 for r in allr if r["kind"] == "SELL" and r["ts"] <= end_ts)
    counts = {}
    for x in decs: counts[x["decision"]] = counts.get(x["decision"], 0) + 1
    return {"date": str(d), "equity_myr": round(st["equity"], 4), "cash_myr": round(st["cash"], 4), "positions_value_myr": round(st["positions_value"], 4),
            "day_pnl_myr": round(day_pnl, 4), "flows_myr": round(st["flows"], 4), "realized_pnl_cumulative_myr": round(realized_cum, 4),
            "holdings": st["holdings"], "trades_today": trades, "stale_symbols": st["stale"], "scan_date": sd, "decision_counts": counts,
            "top_candidates": decs[:5], "ledger_rows": len([r for r in allr if r["ts"] <= end_ts]),
            "data_label": "yfinance delayed/EOD - not real-time", "label": "FORWARD PAPER"}

def _narr(f):
    s = [f"On {f['date']} equity was RM{f['equity_myr']:,.2f} (cash RM{f['cash_myr']:,.2f}); day P&L RM{f['day_pnl_myr']:,.2f}.",
         f"{len(f['holdings'])} open position(s); {len(f['trades_today'])} fill(s) today."]
    if any(x["score"] is not None for x in f["top_candidates"]): s.append("Top scored: " + ", ".join(f"{x['symbol']} {x['score']:.0f} ({x['decision']})" for x in f["top_candidates"] if x["score"] is not None) + ".")
    if f["stale_symbols"]: s.append(f"WARNING stale prices for: {', '.join(f['stale_symbols'])}.")
    return " ".join(s) + "\n" + TEMPLATE_LABEL

def daily_report(now=None, store=True, d: date | None = None):
    now = now or datetime.now(timezone.utc); d = d or now.astimezone(ledger.MYT).date()
    facts = daily_facts(d); h = hashlib.sha256(json.dumps(facts, sort_keys=True, default=str).encode()).hexdigest()
    rep = {"facts": facts, "hash": h, "narrative": _narr(facts)}
    if store:
        c = db.conn(); c.execute("INSERT INTO reports(kind,period,ts,hash,body) VALUES('daily',?,?,?,?) ON CONFLICT(kind,period) DO UPDATE SET ts=excluded.ts,hash=excluded.hash,body=excluded.body",
                                 (str(d), ledger.iso(now), h, json.dumps(rep, default=str))); c.commit(); c.close()
    return rep

def weekly_review(now=None, store=True):
    now = now or datetime.now(timezone.utc); end = now.astimezone(ledger.MYT).date(); start = end - timedelta(days=6)
    a, b = ledger.state_asof(start - timedelta(days=1)), ledger.state_asof(end)
    allr = ledger.rows(); wk = [r for r in allr if r["kind"] in ("BUY", "SELL") and start <= ledger.myt_date(r["ts"]) <= end]
    sells = [r for r in wk if r["kind"] == "SELL"]
    facts = {"week": f"{start}..{end}", "equity_start": round(a["equity"], 2), "equity_end": round(b["equity"], 2),
             "pnl_myr": round(b["equity"] - a["equity"] - b["flows"], 2), "buys": len([r for r in wk if r["kind"] == "BUY"]), "sells": len(sells),
             "realized_myr": round(sum(r["realized_pnl_myr"] for r in sells), 2), "fees_myr": round(sum(r["fees_myr"] for r in wk), 2),
             "wins": len([r for r in sells if r["realized_pnl_myr"] > 0]), "losses": len([r for r in sells if r["realized_pnl_myr"] <= 0])}
    rep = {"facts": facts, "narrative": f"Week {facts['week']}: P&L RM{facts['pnl_myr']}, {facts['buys']} buys, {facts['sells']} sells, realized RM{facts['realized_myr']}, fees RM{facts['fees_myr']}.\n" + TEMPLATE_LABEL}
    h = hashlib.sha256(json.dumps(facts, sort_keys=True).encode()).hexdigest(); rep["hash"] = h
    if store:
        c = db.conn(); c.execute("INSERT INTO reports(kind,period,ts,hash,body) VALUES('weekly',?,?,?,?) ON CONFLICT(kind,period) DO UPDATE SET ts=excluded.ts,hash=excluded.hash,body=excluded.body",
                                 (str(end), ledger.iso(now), h, json.dumps(rep))); c.commit(); c.close()
    return rep
