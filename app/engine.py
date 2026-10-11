"""Daily workflow: collect -> regime -> screen -> rank -> validate -> simulated execution -> monitor -> reconcile -> report.
A score NEVER buys by itself: BUY requires every check below to pass."""
import json, math
from datetime import datetime, timezone, timedelta
import numpy as np, pandas as pd
from . import db, config, data, ledger, risk, scoring, calendars as cal, perf, reports

SNAP_KEYS = ["close", "open", "high", "low", "volume", "sma20", "sma50", "sma150", "sma200", "sma50_slope", "sma200_slope", "ema20", "ema50",
             "macd", "macd_sig", "macd_hist", "rsi14", "atr14", "atr_pct", "vol_ratio", "adv20_local", "r63", "r126", "r252", "vol20", "vol252",
             "hi252", "lo252", "dd_from_hi", "hh20_prev", "res_prev252", "rs126", "rs252"]

def ready():
    db.init(); ledger.init_account()

def universe(cfg, market=None):
    ms = [market] if market else cfg["markets_enabled"]
    syms = []
    for m in ms: syms += cfg["watchlist"].get(m, [])
    return list(dict.fromkeys(syms))

def benchmarks(cfg, market=None):
    ms = [market] if market else cfg["markets_enabled"]
    return list(dict.fromkeys([cfg["benchmarks"][m] for m in ms] + ([cfg["equity_benchmark"]] if not market else [])))

def _now(now): return now or datetime.now(timezone.utc)
def _j(x): return json.dumps(x, default=lambda o: None if (isinstance(o, float) and math.isnan(o)) else (o.item() if hasattr(o, "item") else str(o)), allow_nan=False) if False else json.dumps(_clean(x))
def _clean(o):
    if isinstance(o, dict): return {str(k): _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)): return [_clean(v) for v in o]
    if isinstance(o, (np.floating, float)): return None if (math.isnan(o) or math.isinf(o)) else float(o)
    if isinstance(o, (np.integer,)): return int(o)
    if isinstance(o, (np.bool_,)): return bool(o)
    if isinstance(o, (pd.Timestamp, datetime)): return str(o)
    return o

def regime_for(market, now=None):
    b = config.DEFAULTS["benchmarks"][market]; b = db.get_settings()["benchmarks"][market]
    df = data.load_bars(b)
    st = data.data_status(b, now, df=df)
    if st["status"] not in ("OK", "INSUFFICIENT") and st["status"] != "OK":
        return {"regime": "unknown", "reason": f"benchmark data {st['status']}: {st['reason']}", "benchmark": b}
    if len(df) < 210: return {"regime": "unknown", "reason": "benchmark history insufficient", "benchmark": b}
    f = scoring.feature_frame(df).iloc[-1]
    r = scoring.classify_regime(f); r["benchmark"] = b; r["date"] = str(df.index[-1].date()); return r

def pending_buy_reserved():
    c = db.conn()
    rs = c.execute("SELECT * FROM orders WHERE side='BUY' AND status='pending'").fetchall(); c.close()
    return rs

def create_order(*, decision_key, decision_id, symbol, market, side, qty, ref_price, stop, target, reason, now, needs_approval=False):
    """Idempotent: unique decision_key. Returns order id or None if it already exists."""
    c = db.conn()
    try:
        cur = c.execute("INSERT INTO orders(decision_key,decision_id,created_ts,updated_ts,symbol,market,side,qty,ref_price,stop,target,status,reason,needs_approval)"
                        " VALUES(?,?,?,?,?,?,?,?,?,?,?, 'pending',?,?)",
                        (decision_key, decision_id, ledger.iso(now), ledger.iso(now), symbol, market, side, qty, ref_price, stop, target, reason, int(needs_approval)))
        oid = cur.lastrowid
        c.execute("INSERT INTO order_events(order_id,ts,status,note) VALUES(?,?,?,?)", (oid, ledger.iso(now), "pending", reason))
        c.commit(); return oid
    except Exception as e:
        if "UNIQUE" in str(e): return None
        raise
    finally: c.close()

def set_order(oid, status, note, now, **kw):
    c = db.conn(); sets = ",".join(f"{k}=?" for k in kw)
    c.execute(f"UPDATE orders SET status=?, reason=?, updated_ts=?{',' + sets if sets else ''} WHERE id=?", [status, note, ledger.iso(now), *kw.values(), oid])
    c.execute("INSERT INTO order_events(order_id,ts,status,note) VALUES(?,?,?,?)", (oid, ledger.iso(now), status, note)); c.commit(); c.close()

# ---------------------------------------------------------------- scan
def analyze_universe(symbols, market, now):
    cfg = db.get_settings(); bench = data.load_bars(cfg["benchmarks"][market])
    frames, status = {}, {}
    for s in symbols:
        df = data.load_bars(s); st = data.data_status(s, now, df=df); status[s] = st
        if st["status"] == "OK": frames[s] = scoring.feature_frame(df, bench["close"] if len(bench) else None)
    rs = {s: (f["rs252"].iloc[-1] if not pd.isna(f["rs252"].iloc[-1]) else f["rs126"].iloc[-1]) for s, f in frames.items()}
    ser = pd.Series(rs).dropna(); pct = (ser.rank(pct=True) * 100).to_dict() if len(ser) >= 5 else {}
    return frames, status, pct

def scan(provider, now=None, market=None, trade=True, refresh=True):
    """Full EOD workflow for one market (or all enabled). Idempotent for repeated runs on the same data."""
    now = _now(now); ready(); cfg = db.get_settings(); summary = {"started": ledger.iso(now), "markets": {}, "data_mode": provider.mode}
    markets = [market] if market else cfg["markets_enabled"]
    db.event("INFO", "scan", f"scan start markets={markets} data_mode={provider.mode}", ledger.iso(now))
    # 1. data collection
    syms = universe(cfg, market) + benchmarks(cfg, market) if market else universe(cfg) + benchmarks(cfg)
    if provider.mode == "DEMO":
        return demo_scan(provider, markets, cfg, now)
    if refresh:
        ok, missing = data.refresh(provider, list(dict.fromkeys(syms + [cfg["equity_benchmark"]])), now)
        summary["data"] = {"symbols_ok": ok, "missing": missing}
        if ok == 0:
            db.event("ERROR", "scan", "NO LIVE DATA AT ALL - scan aborted (no trades). Use DEMO mode only for demonstration.", ledger.iso(now))
            summary["aborted"] = "no live data"; return summary
    # 6b position monitoring uses fresh bars -> before new entries
    update_positions_eod(now)
    for m in markets:
        if not cal.is_session_day(m, cal.last_completed_session(m, now)): continue
        summary["markets"][m] = scan_market(m, cfg, provider, now, trade)
    rec = reconcile(now); summary["reconciliation"] = rec
    summary["report"] = reports.daily_report(now, store=True)["facts"]["date"]
    db.event("INFO", "scan", f"scan complete: {json.dumps({m: v.get('counts') for m, v in summary['markets'].items()})}", ledger.iso(now))
    db.heartbeat("last_scan", ledger.iso(now))
    return summary

def scan_market(m, cfg, provider, now, trade=True):
    sd = cal.last_completed_session(m, now); symbols = universe(cfg, m)
    reg = regime_for(m, now); frames, status, pct = analyze_universe(symbols, m, now)
    fxr = data.get_fx(now); res = []; counts = {"BUY": 0, "WATCH": 0, "REJECT": 0, "BLOCKED_DATA": 0}
    held = ledger.positions(); eq = ledger.equity_now(now)
    pend = pending_buy_reserved()
    reserved_cash = sum(o["qty"] * o["ref_price"] * (1 if o["market"] == "BURSA" else (fxr["rate"] or 1)) for o in pend)
    n_pos = len(held) + len(pend); exposure = eq["positions_value"] + reserved_cash
    cands = []
    for s in symbols:
        st = status[s]
        if s not in frames:
            cands.append((-1, s, {"blocked": True, "st": st})); continue
        f = frames[s].iloc[-1]
        fund = data.get_fundamentals(s, provider, now)
        fx = 1.0 if m == "BURSA" else (fxr["rate"] or 1.0)
        comp = scoring.composite(f, fund=fund, regime=reg["regime"], fx=fx, rs_pct=pct.get(s))
        cands.append((comp["score"], s, {"f": f, "fund": fund, "comp": comp, "st": st, "fx": fx}))
    cands.sort(key=lambda x: -x[0])
    for score, s, c in cands:                      # rank stage: best first so limited slots go to best candidates
        key_last = c["st"].get("last_bar", "none")
        key = f"{config.ALGO_VERSION}:{m}:{sd}:{s}:{key_last}"
        if c.get("blocked"):
            d = {"decision": "REJECT", "score": None, "band": "n/a", "checks": [{"name": "data_fresh", "pass": False, "detail": f"{c['st']['status']}: {c['st']['reason']}"}],
                 "plan": None, "scores": None, "snapshot": {"data_status": c["st"]}, "explanation": f"DATA BLOCKED ({c['st']['status']}): {c['st']['reason']}. No trade possible for {s}."}
            counts["BLOCKED_DATA"] += 1
        else:
            d = decide(s, m, c, reg, cfg, eq, n_pos, exposure, held, pend, fxr, now)
            counts[d["decision"]] += 1
            if d["decision"] == "BUY":
                n_pos += 1; exposure += d["plan"]["qty"] * d["plan"]["entry"] * c["fx"]
        did = save_decision(key, now, sd, s, m, d, provider.mode)
        d["id"] = did; d["symbol"] = s
        if d["decision"] == "BUY" and trade and cfg["auto_trade"] and did:
            p = d["plan"]
            oid = create_order(decision_key=key, decision_id=did, symbol=s, market=m, side="BUY", qty=p["qty"], ref_price=p["entry"], stop=p["stop"],
                               target=p["target"], reason=f"scan {sd} score {d['score']}", now=now, needs_approval=cfg["manual_approval"])
            if oid:
                memo = reports.make_memo(did, s, d, now); d["order_id"] = oid; d["memo"] = memo
                db.event("INFO", "order", f"BUY order #{oid} pending {s} x{p['qty']} (ref {p['entry']:.3f}, stop {p['stop']:.3f}){' - AWAITING MANUAL APPROVAL' if cfg['manual_approval'] else ''}", ledger.iso(now))
        res.append({"symbol": s, "decision": d["decision"], "score": d["score"]})
    return {"session_date": str(sd), "regime": reg, "counts": counts, "results": res}

def save_decision(key, now, sd, s, m, d, mode):
    c = db.conn()
    try:
        cur = c.execute("INSERT INTO decisions(decision_key,ts,scan_date,symbol,market,algo_version,data_mode,decision,score,band,snapshot,scores,checks,plan,explanation)"
                        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (key, ledger.iso(now), str(sd), s, m, config.ALGO_VERSION, mode, d["decision"], d["score"], d["band"], _j(d["snapshot"]),
                         _j(d["scores"]), _j(d["checks"]), _j(d["plan"]), d["explanation"]))
        c.commit(); return cur.lastrowid
    except Exception as e:
        if "UNIQUE" in str(e): return None     # same decision key already recorded -> idempotent
        raise
    finally: c.close()

def decide(s, m, c, reg, cfg, eq, n_pos, exposure, held, pend, fxr, now):
    f, comp, fund, fx = c["f"], c["comp"], c["fund"], c["fx"]
    score = comp["score"]; checks = []
    add = lambda name, ok, detail: checks.append({"name": name, "pass": bool(ok), "detail": detail})
    add("data_fresh", True, f"last bar {c['st']['last_bar']} ({c['st']['latency']})")
    if m == "US": add("fx_available", fxr["ok"], f"USDMYR {fxr['rate']} as of {fxr['date']} ({fxr['status']})")
    add("regime_allows_entries", reg["regime"] in cfg["allowed_regimes"], f"{m} regime {reg['regime']}: {reg.get('reason')}")
    thr = cfg["buy_min_score"] if reg["regime"] == "bullish" else cfg["sideways_min_score"]
    add("score_threshold", score >= thr, f"score {score} vs required {thr} (regime {reg['regime']})")
    add("minervini_trend_template", comp["minervini"]["pass"], f"{comp['minervini']['passed']}/8" + (f", unknown: {comp['minervini']['unknown']}" if comp["minervini"]["unknown"] else ""))
    trig = scoring.entry_trigger(f); add("entry_trigger", trig["fired"], trig["reason"])
    plan = scoring.plan_levels(f, cfg, (fund or {}).get("analyst_target")) if not pd.isna(f["atr14"]) else None
    if cfg.get("weekly_entries_only", False):
        sd_ = cal.last_completed_session(m, now); add("weekly_swing_window", pd.Timestamp(sd_).weekday() == 4, f"session {sd_} ({pd.Timestamp(sd_).day_name()}); new entries only on the week's closing session (Fri)")
    add("not_blocked_manual", s not in cfg["blocked_symbols"], "manual block list")
    add("not_held_or_pending", s not in held and not any(o["symbol"] == s for o in pend), "no duplicate position/order")
    qty = 0; sz = None
    if plan:
        if plan["rr"] is not None: add("reward_to_risk>=min", plan["rr"] >= cfg["min_rr"], f"R:R {plan['rr']:.2f} vs {cfg['min_rr']} (target {plan['target']:.3f} from {plan['target_source']})")
        else: add("reward_to_risk>=min", True, "no credible target exists: not invented; exit relies on ATR trailing stop")
        sz = risk.size_position(equity=eq["equity"], cash=eq["cash"], price=plan["entry"], stop=plan["stop"], market=m, fx=fx, cfg=cfg,
                                adv_local=float(f["adv20_local"]) if not pd.isna(f["adv20_local"]) else None, current_exposure=exposure, n_positions=n_pos)
        qty = sz["qty"]; add("position_size>=1_lot", qty >= cfg["lot_size"][m], f"qty {qty} (binding cap: {sz['binding']}; caps {sz['detail'].get('caps')})")
        cost = qty * plan["entry"] * fx
        for nme, ok, det in risk.portfolio_checks(cfg=cfg, equity=eq["equity"], cash=eq["cash"], n_positions=n_pos, exposure_after=exposure + cost, cost_myr=cost,
                                                  suspended=cfg["entries_suspended"]): add(nme, ok, det)
        adv = f["adv20_local"] * fx if not pd.isna(f["adv20_local"]) else 0
        add("execution_quality", adv >= 1e6 and f["atr_pct"] <= 0.08, f"ADV RM{adv:,.0f} (min RM1,000,000), ATR% {f['atr_pct']*100:.2f} (max 8)")
    else: add("plan_available", False, "ATR unavailable")
    add("auto_trade_enabled", cfg["auto_trade"], "auto-trade ON" if cfg["auto_trade"] else "auto-trade OFF: decision only")
    hard = [k["name"] for k in checks if not k["pass"] and k["name"] != "auto_trade_enabled"]
    if not hard and score >= 70: decision = "BUY"
    elif score >= 70: decision = "WATCH"
    else: decision = "REJECT"
    if decision == "BUY" and not cfg["auto_trade"]: decision = "BUY"  # signal remains BUY; order creation gated separately
    plan_out = None
    if plan: plan_out = {**plan, "qty": qty, "fx": fx, "size_detail": sz["detail"] if sz else None, "est_cost_myr": qty * plan["entry"] * fx}
    expl = reports.explain_decision(s, decision, score, comp, checks, plan_out, hard)
    snap = {"bar_date": c["st"]["last_bar"], "features": {k: (None if pd.isna(f.get(k)) else float(f[k])) for k in SNAP_KEYS if k in f.index},
            "fundamentals": fund, "fx": fxr, "regime": reg, "data_status": c["st"], "data_label": data.DATA_LABEL,
            "equity_at_decision": eq["equity"], "cash_at_decision": eq["cash"]}
    return {"decision": decision, "score": score, "band": scoring.band(score), "checks": checks, "plan": plan_out,
            "scores": {"factors": comp["factors"], "weights": scoring.W, "weights_used": comp["weights_used"], "flags": comp["flags"],
                       "minervini": comp["minervini"], "canslim": comp["canslim"], "graham": comp["graham"]},
            "snapshot": snap, "explanation": expl}

def demo_scan(provider, markets, cfg, now):
    """DEMO mode: synthetic data, never persisted as prices, no orders, no ledger changes."""
    db.event("WARN", "scan", "DEMO MODE: no live data - synthetic data shown, trading disabled", ledger.iso(now))
    return {"data_mode": "DEMO", "warning": "SYNTHETIC DATA - NOT REAL PRICES - NO TRADES", "markets": {}}

# ---------------------------------------------------------------- positions / exits
def update_positions_eod(now):
    """Trailing stop ratchet + exit signals using FINAL bars only. Exit orders fill at next open."""
    cfg = db.get_settings(); pos = ledger.positions(); c = db.conn()
    for s, p in pos.items():
        df = data.load_bars(s); st = data.data_status(s, now, df=df)
        ps = c.execute("SELECT * FROM position_state WHERE symbol=?", (s,)).fetchone()
        if ps is None or len(df) < 20: continue
        if st["status"] != "OK":
            db.event("WARN", "monitor", f"{s}: data {st['status']} - stop cannot be updated ({st['reason']})", ledger.iso(now)); continue
        f = scoring.feature_frame(df).iloc[-1]; entry_d = pd.Timestamp(ps["entry_ts"][:10])
        post = df[df.index >= entry_d.tz_localize(None)] if len(df) else df
        hi = max(ps["highest_close"] or 0, float(post["close"].max())) if len(post) else ps["highest_close"]
        new_stop = max(ps["stop"], hi - cfg["atr_stop_mult"] * float(f["atr14"]))
        c.execute("UPDATE position_state SET highest_close=?, stop=?, atr=? WHERE symbol=?", (hi, new_stop, float(f["atr14"]), s)); c.commit()
        last_d = str(df.index[-1].date())
        reason = None
        if df["close"].iloc[-1] <= ps["stop"]: reason = f"close {df['close'].iloc[-1]:.3f} <= stop {ps['stop']:.3f}"
        elif ps["target"] and df["close"].iloc[-1] >= ps["target"]: reason = f"close >= target {ps['target']:.3f}"
        elif not pd.isna(f["sma50"]) and f["close"] < f["sma50"]: reason = f"trend break: close {f['close']:.3f} < SMA50 {f['sma50']:.3f}"
        if reason:
            oid = create_order(decision_key=f"EXIT:{s}:{last_d}", decision_id=None, symbol=s, market=p["market"], side="SELL", qty=p["qty"],
                               ref_price=float(df["close"].iloc[-1]), stop=None, target=None, reason=reason, now=now)
            if oid: db.event("INFO", "monitor", f"EXIT order #{oid} pending for {s}: {reason}", ledger.iso(now))
    c.close()

def execute_pending(provider, now=None, refresh=True) -> list:
    """Simulated execution. Only while the market is open; fills at the session's open from a FRESH (today's) delayed bar.
    Also evaluates intraday stop hits (fill at stop, or at open if the open gapped through it)."""
    now = _now(now); cfg = db.get_settings(); log = []
    c = db.conn(); pend = [dict(r) for r in c.execute("SELECT * FROM orders WHERE status='pending' ORDER BY id")]; c.close()
    held = ledger.positions(); markets_open = {m for m in cal.MARKETS if cal.is_open(m, now)}
    watch = {o["symbol"] for o in pend if o["market"] in markets_open} | {s for s, p in held.items() if p["market"] in markets_open}
    if refresh and watch and provider.mode == "LIVE":
        try:
            bars = provider.daily(list(watch) + [data.FX_SYMBOL], period="5d"); data.save_bars(bars, now); data.set_source("yfinance-intraday", True, f"{len(bars)} symbols (delayed)")
        except Exception as e:
            data.set_source("yfinance-intraday", False, str(e)); db.event("ERROR", "monitor", f"intraday refresh failed: {e}", ledger.iso(now))
    for o in pend:
        m = o["market"]
        if (now - datetime.fromisoformat(o["created_ts"])).days >= cfg["pending_expiry_days"]:
            set_order(o["id"], "canceled", "expired unfilled", now); log.append((o["id"], "canceled", "expired")); continue
        if provider.mode == "DEMO": log.append((o["id"], "blocked", "DEMO data: execution disabled")); continue
        if not cal.is_open(m, now): log.append((o["id"], "blocked", f"{m} market closed")); continue
        if o["needs_approval"] and not o["approved"]: log.append((o["id"], "blocked", "awaiting manual approval")); continue
        today = cal.local_now(m, now).date()
        df = data.load_bars(o["symbol"], include_partial=True)
        if len(df) == 0 or df.index[-1].date() != today:
            log.append((o["id"], "blocked", "no fresh bar for today (data missing/stale) - no trade")); continue
        bar = df.iloc[-1]
        if not (bar.open > 0): log.append((o["id"], "blocked", "invalid open price")); continue
        res = _fill_order(o, float(bar.open), now, cfg); log.append((o["id"],) + res)
    # intraday stops
    for s, p in ledger.positions().items():
        m = p["market"]
        if not cal.is_open(m, now) or provider.mode == "DEMO": continue
        c = db.conn(); ps = c.execute("SELECT * FROM position_state WHERE symbol=?", (s,)).fetchone(); c.close()
        df = data.load_bars(s, include_partial=True); today = cal.local_now(m, now).date()
        if ps is None or len(df) == 0 or df.index[-1].date() != today: continue
        bar = df.iloc[-1]
        if bar.low <= ps["stop"]:
            fillpx = min(float(bar.open), ps["stop"])          # gapped through stop => fill at open
            gap = bar.open < ps["stop"]
            oid = create_order(decision_key=f"STOP:{s}:{today}", decision_id=None, symbol=s, market=m, side="SELL", qty=p["qty"], ref_price=fillpx,
                               stop=None, target=None, reason=f"stop {ps['stop']:.3f} hit intraday" + (" (gap through stop: filled at open)" if gap else ""), now=now)
            if oid:
                o = {"id": oid, "symbol": s, "market": m, "side": "SELL", "qty": p["qty"], "stop": None, "target": None, "ref_price": fillpx, "reason": "stop"}
                res = _fill_order(o, fillpx, now, cfg, note="STOP" + (" GAP" if gap else "")); log.append((oid,) + res)
    if any(r[1] == "filled" for r in log): reconcile(now)
    return log

def _fill_order(o, open_px, now, cfg, note=""):
    m, s = o["market"], o["symbol"]
    try:
        if o["side"] == "BUY":
            if s in cfg["blocked_symbols"]: raise _Reject("symbol manually blocked")
            if cfg["entries_suspended"]: raise _Reject("new entries suspended (drawdown limit)")
            st = data.data_status(s, now)
            if st["status"] != "OK": raise _Reject(f"data {st['status']}: {st['reason']}")
            if open_px > o["ref_price"] * (1 + cfg["max_gap_chase_pct"] / 100): raise _Reject(f"open {open_px:.3f} gapped >{cfg['max_gap_chase_pct']}% above planned entry {o['ref_price']:.3f}")
            stop = o["stop"]
            if stop is None or open_px <= stop: raise _Reject(f"open {open_px:.3f} at/below stop {stop}")
            eq = ledger.equity_now(now); fxr = data.get_fx(now); fx = 1.0 if m == "BURSA" else fxr["rate"]
            if fx is None or (m == "US" and not fxr["ok"]): raise _Reject("FX stale/missing")
            held_val = eq["positions_value"]
            if len(ledger.positions()) >= cfg["max_positions"]: raise _Reject("max positions reached")
            df = data.load_bars(s); adv = float((df["close"] * df["volume"]).tail(20).mean())
            sz = risk.size_position(equity=eq["equity"], cash=eq["cash"], price=open_px, stop=stop, market=m, fx=fx, cfg=cfg, adv_local=adv, current_exposure=held_val)
            qty = min(o["qty"], sz["qty"])
            if qty < cfg["lot_size"][m]: raise _Reject(f"resized qty {qty} < 1 lot after live checks (binding {sz['binding']})")
            fl = ledger.execute_fill(order_id=o["id"], symbol=s, market=m, side="BUY", qty=qty, raw_price=open_px, now=now, note=f"order#{o['id']}", cfg=cfg)
            set_order(o["id"], "filled", f"filled {qty} @ {fl['price']:.4f}", now, fill_price=fl["price"], fill_ts=ledger.iso(now), qty=qty)
            c = db.conn()
            atr = None
            df2 = data.load_bars(s)
            if len(df2) > 20: atr = float(scoring.feature_frame(df2).iloc[-1]["atr14"])
            memo = c.execute("SELECT id FROM memos WHERE decision_id=?", (o["decision_id"],)).fetchone()
            c.execute("INSERT OR REPLACE INTO position_state VALUES(?,?,?,?,?,?,?,?,?,?)", (s, m, stop, stop, o["target"], fl["price"], atr, ledger.iso(now), o["decision_id"], memo["id"] if memo else None))
            c.commit(); c.close()
            db.event("INFO", "execution", f"FILLED BUY {s} x{qty} @ {fl['price']:.4f} (fees RM{fl['fees_myr']:.2f}, slippage RM{fl['slippage_myr']:.2f})", ledger.iso(now))
            return ("filled", f"{qty}@{fl['price']:.4f}")
        else:
            p = ledger.positions().get(s)
            if not p: raise _Reject("no position to sell")
            qty = min(o["qty"], p["qty"])
            fl = ledger.execute_fill(order_id=o["id"], symbol=s, market=m, side="SELL", qty=qty, raw_price=open_px, now=now, note=note or f"order#{o['id']}: {o.get('reason','')}"[:200], cfg=cfg)
            set_order(o["id"], "filled", f"filled {qty} @ {fl['price']:.4f}", now, fill_price=fl["price"], fill_ts=ledger.iso(now))
            if s not in ledger.positions():
                c = db.conn(); c.execute("DELETE FROM position_state WHERE symbol=?", (s,)); c.commit(); c.close()
            db.event("INFO", "execution", f"FILLED SELL {s} x{qty} @ {fl['price']:.4f} realized RM{fl['realized_pnl_myr']:.2f} {note}", ledger.iso(now))
            return ("filled", f"{qty}@{fl['price']:.4f}")
    except _Reject as e:
        set_order(o["id"], "rejected", str(e), now); db.event("WARN", "execution", f"order #{o['id']} {s} REJECTED: {e}", ledger.iso(now)); return ("rejected", str(e))
    except Exception as e:
        set_order(o["id"], "rejected", f"error: {e}", now); db.event("ERROR", "execution", f"order #{o['id']} {s} error: {e}", ledger.iso(now)); return ("rejected", str(e))

class _Reject(Exception): pass

def approve_order(oid, now=None):
    c = db.conn(); c.execute("UPDATE orders SET approved=1 WHERE id=? AND status='pending'", (oid,)); c.commit(); c.close()
    db.event("INFO", "order", f"order #{oid} manually approved (fills at next open)", ledger.iso(_now(now)))

def cancel_order(oid, now=None):
    set_order(oid, "canceled", "canceled manually", _now(now))

def manual_close(symbol, now=None):
    p = ledger.positions().get(symbol)
    if not p: raise ValueError("no such position")
    d = _now(now).astimezone(cal.tz(p["market"])).date()
    oid = create_order(decision_key=f"MANUAL_EXIT:{symbol}:{d}", decision_id=None, symbol=symbol, market=p["market"], side="SELL", qty=p["qty"],
                       ref_price=0.0, stop=None, target=None, reason="manual close (fills at next open)", now=_now(now))
    return oid

# ---------------------------------------------------------------- reconcile / drawdown
def reconcile(now=None) -> dict:
    now = _now(now); curve = ledger.rebuild_equity_curve(now); pos = ledger.positions(); c = db.conn()
    states = {r["symbol"] for r in c.execute("SELECT symbol FROM position_state")}
    for s in states - set(pos): c.execute("DELETE FROM position_state WHERE symbol=?", (s,))
    c.commit(); c.close()
    missing = sorted(set(pos) - states)
    eq = ledger.equity_now(now)
    out = {"ledger_cash": eq["cash"], "positions_value": eq["positions_value"], "equity": eq["equity"], "orphan_states_removed": sorted(states - set(pos)),
           "positions_without_stop_state": missing, "ok": not missing, "stale_symbols": eq["stale"]}
    out["drawdown"] = check_drawdown(now)
    db.event("INFO" if out["ok"] else "WARN", "reconcile", f"equity RM{eq['equity']:.2f} cash RM{eq['cash']:.2f} positions {len(pos)}" + (f" MISSING STOP STATE {missing}" if missing else ""), ledger.iso(now))
    return out

def check_drawdown(now=None):
    cfg = db.get_settings(); df = perf.curve_df()
    if len(df) < 2: return {"drawdown_pct": 0.0, "suspended": cfg["entries_suspended"]}
    if cfg["dd_baseline_ts"]: df = df[df.index >= pd.Timestamp(cfg["dd_baseline_ts"][:10])]
    nav = perf.nav_series(df)
    dd = float(1 - nav.iloc[-1] / nav.cummax().iloc[-1]) * 100 if len(nav) else 0.0
    if dd >= cfg["max_drawdown_pct"] and not cfg["entries_suspended"]:
        db.set_settings({"entries_suspended": True, "suspend_reason": f"drawdown {dd:.2f}% >= limit {cfg['max_drawdown_pct']}%"})
        db.event("CRITICAL", "risk", f"NEW ENTRIES SUSPENDED: drawdown {dd:.2f}% >= {cfg['max_drawdown_pct']}%. Manual authorization required to resume.", ledger.iso(_now(now)))
    return {"drawdown_pct": dd, "suspended": db.get_settings()["entries_suspended"]}

def resume_entries(now=None):
    """Manual authorization: clears the suspension and re-baselines drawdown measurement from today."""
    now = _now(now)
    db.set_settings({"entries_suspended": False, "suspend_reason": "", "dd_baseline_ts": ledger.iso(now)})
    db.event("WARN", "risk", "Entries RESUMED by manual authorization; drawdown baseline reset", ledger.iso(now))
