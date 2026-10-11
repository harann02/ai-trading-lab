"""v2 US-only orchestrator: discover universe -> refresh data -> regime -> allocate capital (real FX conversions) -> run independent strategy engines ->
simulated execution (next open, delayed data) -> reconcile -> reports. The deterministic engine has sole execution authority; the AI layer only explains."""
import json, math, traceback
from datetime import datetime, timezone, timedelta
import numpy as np, pandas as pd
from . import db, config, data, ledger, risk, scoring, universe, fx as fxm, portfolio, calendars as cal, engine, reports, perf
from .providers import registry, base as pb
from .strategies import common as C, swing, meanrev, baseline, intraday

ENGINES = {"swing": swing, "meanrev": meanrev, "baseline": baseline}

def _now(now): return now or datetime.now(timezone.utc)

# ------------------------------------------------------------------ capital allocation (no double allocation)
def validate_allocations(cfg):
    tot = sum(float(v["allocation_pct"]) for v in cfg["strategies"].values())
    if tot > 100.0 + 1e-9: raise ValueError(f"strategy allocations sum to {tot}% (>100%): cash cannot be allocated twice")
    return tot

def ensure_allocations(now, cfg=None, rate=None):
    """Converts MYR treasury -> USD sleeves per allocation_pct of NET contributed capital (idempotent: only the missing amount is converted, at a real recorded rate + spread)."""
    cfg = cfg or db.get_settings(); validate_allocations(cfg); out = []
    rows = ledger.rows(); net = sum(r["cash_delta_myr"] for r in rows if r["kind"] in ("DEPOSIT", "WITHDRAW"))
    c = db.conn(); done = {r["strategy_id"]: r["m"] for r in c.execute("SELECT strategy_id, SUM(myr_amount) m FROM fx_conversions WHERE direction='MYR->USD' GROUP BY strategy_id")}; c.close()
    for sid, st in cfg["strategies"].items():
        if not st["enabled"]: continue
        target = net * st["allocation_pct"] / 100.0; missing = target - (done.get(sid) or 0.0)
        if missing >= 1.0:
            rate = rate or fxm.fetch_rate(cfg, now)
            if not rate.get("ok"): db.event("ERROR", "fx", f"cannot allocate to {sid}: FX unavailable ({rate})", ledger.iso(now)); continue
            amt = min(missing, ledger.cash())
            if amt >= 1.0:
                r = fxm.convert_myr_to_usd(sid, round(amt, 2), now, cfg, rate=rate, note=f"allocation {st['allocation_pct']}% of RM{net:,.2f}", uniq=f"FX:ALLOC:{sid}:{int(round((done.get(sid) or 0)))}->{int(round(target))}")
                if r: out.append({"strategy": sid, **r}); db.event("INFO", "fx", f"allocated RM{amt:,.2f} to {sid}: USD{r['usd']:,.2f} @ {r['effective']:.5f} (mid {r['mid']:.5f}, spread RM{r['spread_cost_myr']:.2f})", ledger.iso(now))
    return out

# ------------------------------------------------------------------ context passed to strategy engines
class Ctx:
    def __init__(self, **kw):
        self.__dict__.update(kw); self._pend = None; self._reserved = {}
        self.n_pos = len(self.snap["holdings"])
    def held_syms(self, sid): return [h["symbol"] for h in self.snap["holdings"] if h["strategy_id"] == sid]
    def pend_syms(self, sid):
        if self._pend is None: self._pend = C.pending_orders()
        return [o["symbol"] for o in self._pend if o["strategy_id"] == sid and o["side"] == "BUY"]
    def bars(self, s): return self.dfs[s]
    def pos_state(self, sid, s):
        c = db.conn(); r = c.execute("SELECT * FROM position_state2 WHERE strategy_id=? AND symbol=?", (sid, s)).fetchone(); c.close(); return dict(r) if r else None
    def fundamentals(self, s):
        try: return data.get_fundamentals(s, self.provider, self.now)
        except Exception: return None
    def reserve(self, sid, usd):
        sl = self.snap["sleeves"].get(sid)
        if sl: sl["buying_power_usd"] = max(0.0, sl["buying_power_usd"] - usd); sl["cash_usd"] -= usd; sl["positions_usd"] += usd   # pending buys reserve cash/exposure inside this scan

def _pending_reserve(snap):
    """Pending BUY orders from earlier scans reserve cash/exposure so a second scan cannot double-spend the sleeve."""
    for o in C.pending_orders():
        if o["side"] != "BUY": continue
        sl = snap["sleeves"].get(o["strategy_id"]); usd = o["qty"] * o["ref_price"]
        if sl: sl["buying_power_usd"] = max(0.0, sl["buying_power_usd"] - usd); sl["cash_usd"] -= usd; sl["positions_usd"] += usd
        snap["holdings"].append({"strategy_id": o["strategy_id"], "symbol": o["symbol"], "pending": True, "value_usd": usd})

# ------------------------------------------------------------------ data
def refresh_data(provider, symbols, now, keep_bars=300, held=()):
    """Downloads daily bars in batches; trims to keep_bars (held/benchmarks keep more). Records source status. Returns (n_ok, missing)."""
    syms = list(dict.fromkeys(list(symbols) + [data.FX_SYMBOL])); got = {}
    for i in range(0, len(syms), 80):
        chunk = syms[i:i + 80]
        for attempt in range(2):
            try:
                b = provider.daily(chunk, period="2y"); got.update(b)
                missing = [s for s in chunk if s not in b]
                if not missing or attempt == 1: break
                chunk = missing
            except Exception as e:
                db.event("WARN", "data", f"batch download failed ({e}); retry {attempt}", ledger.iso(now))
    keep_long = set(held) | {"SPY", "QQQ", data.FX_SYMBOL}
    trimmed = {s: (df if s in keep_long else df.tail(keep_bars)) for s, df in got.items()}
    data.save_bars(trimmed, now)
    missing = [s for s in syms if s not in got]
    data.set_source("yfinance", len(got) > 0, f"{len(got)}/{len(syms)} symbols; missing: {missing[:10]}")
    return len(got), missing

def probe_providers(now, results=None):
    res = results or registry.probe_all(store=True)
    return res

# ------------------------------------------------------------------ scan
def run_scan(provider, now=None, trade=True, refresh=True, probe=True):
    now = _now(now); engine.ready(); cfg = db.get_settings(); validate_allocations(cfg)
    summary = {"started": ledger.iso(now), "platform": config.PLATFORM_VERSION, "provider": getattr(provider, "name", "yfinance"), "strategies": {}}
    db.event("INFO", "scan", f"v2 US scan start (trade={trade})", ledger.iso(now))
    caps = registry.probe_all(store=True) if probe else registry.results_from_store()
    summary["capabilities"] = [r.to_dict() for r in caps]
    sd = cal.last_completed_session("US", now); summary["session"] = str(sd)
    # 1. universe + data
    cand, ex, flags = universe.candidates(cfg, now); summary["universe_sources"] = flags
    held_all = {h["symbol"] for h in portfolio.snapshot(now, cfg)["holdings"]}
    extra = ["SPY", "QQQ"] + list(cfg.get("baseline_symbols", [])) + sorted(held_all)
    if refresh:
        ok, missing = refresh_data(provider, list(cand) + extra, now, held=held_all)
        summary["data"] = {"symbols_ok": ok, "missing": len(missing)}
        if ok == 0: summary["aborted"] = "no live data"; db.event("ERROR", "scan", "NO LIVE DATA - aborted", ledger.iso(now)); return summary
    fxr = fxm.fetch_rate(cfg, now) if refresh else fxm.current(cfg, now); summary["fx"] = fxr
    passed, table, _ = universe.evaluate(cfg, lambda s: data.load_bars(s), now)
    summary["universe"] = {"candidates": len(cand), "passed": len(passed), "rejected": len(cand) - len(passed)}
    # 2. capital
    summary["allocations"] = ensure_allocations(now, cfg, fxr if fxr.get("ok") else None)
    snap = portfolio.snapshot(now, cfg); _pending_reserve(snap)
    # 3. features / regime
    spy = data.load_bars("SPY"); qqq = data.load_bars("QQQ")
    if len(spy) < 210: summary["aborted"] = "SPY history missing"; return summary
    reg = engine.regime_for("US", now); summary["regime"] = reg
    uni = list(dict.fromkeys(passed + sorted(held_all)))
    dfs, status, frames = {}, {}, {}
    for s in uni + ["SPY", "QQQ"]:
        df = data.load_bars(s); dfs[s] = df; st = data.data_status(s, now, df=df); status[s] = st
        if st["status"] == "OK" and s in uni:
            frames[s] = scoring.enrich(scoring.feature_frame(df, spy["close"]), df, spy["close"], qqq["close"] if len(qqq) else None)
    rs = {s: (f["rs252"].iloc[-1] if not pd.isna(f["rs252"].iloc[-1]) else f["rs126"].iloc[-1]) for s, f in frames.items()}
    ser = pd.Series(rs).dropna(); pct = (ser.rank(pct=True) * 100).to_dict() if len(ser) >= 5 else {}
    ctx = Ctx(now=now, cfg=cfg, snap=snap, frames=frames, status=status, dfs=dfs, pct=pct, regime=reg, sd=sd, fx=fxr, trade=trade, universe=uni,
              provider=provider, provider_name=getattr(provider, "name", "yfinance"), data_label=getattr(provider, "LABEL", data.DATA_LABEL))
    # 4. strategies (independent)
    ok_caps = {"eod": any(r.has(pb.EOD) for r in caps) or True}
    for sid, mod in ENGINES.items():
        if not C.is_enabled(cfg, sid, ok_caps["eod"]): summary["strategies"][sid] = {"enabled": False, "reason": "disabled by owner/capability/suspension"}; continue
        try:
            summary["strategies"][sid] = mod.scan(ctx)
        except Exception as e:
            db.event("ERROR", "scan", f"strategy {sid} failed: {e}\n{traceback.format_exc()[-500:]}", ledger.iso(now)); summary["strategies"][sid] = {"error": str(e)}
    summary["strategies"]["intraday"] = {"enabled": False, **intraday.status(cfg, caps)}
    save_scan_results(now, sd, summary["strategies"], universe_table=table, regime=reg)
    # 5. reconcile
    rec = reconcile(now); summary["reconciliation"] = rec
    db.event("INFO", "scan", f"v2 scan complete: " + json.dumps({k: v.get("counts") for k, v in summary["strategies"].items() if isinstance(v, dict) and v.get("counts")}), ledger.iso(now))
    db.heartbeat("last_scan", ledger.iso(now))
    return summary

def save_scan_results(now, sd, strategies, universe_table=None, regime=None):
    c = db.conn()
    c.execute("CREATE TABLE IF NOT EXISTS scan_results(scan_date TEXT, strategy_id TEXT, ts TEXT, payload TEXT, PRIMARY KEY(scan_date,strategy_id))")
    for sid, res in strategies.items():
        if isinstance(res, dict) and "rows" in res:
            c.execute("INSERT OR REPLACE INTO scan_results VALUES(?,?,?,?)", (str(sd), sid, ledger.iso(now), json.dumps({"counts": res["counts"], "rows": res["rows"], "regime": regime}, default=str)))
    c.execute("DELETE FROM scan_results WHERE scan_date NOT IN (SELECT DISTINCT scan_date FROM scan_results ORDER BY scan_date DESC LIMIT 3)")
    c.commit(); c.close()

# ------------------------------------------------------------------ execution (simulated, next open, delayed data)
def _ps_insert(sid, s, stop, target, atr, now, decision_id, reason):
    c = db.conn(); c.execute("INSERT OR REPLACE INTO position_state2 VALUES(?,?,?,?,?,?,?,?,?,?)", (sid, s, stop, stop, target, None, atr, ledger.iso(now), decision_id, reason)); c.commit(); c.close()

def execute_pending(provider, now=None, refresh=True) -> list:
    """Fills pending v2 orders only while NYSE is open and today's (delayed) bar exists; re-validates every gate at fill time; intraday stop checks."""
    now = _now(now); cfg = db.get_settings(); log = []
    c = db.conn(); pend = [dict(r) for r in c.execute("SELECT * FROM orders WHERE status='pending' AND strategy_id IS NOT NULL ORDER BY id")]; c.close()
    snap0 = portfolio.snapshot(now, cfg); opened = cal.is_open("US", now)
    watch = {o["symbol"] for o in pend} | {h["symbol"] for h in snap0["holdings"]}
    if refresh and watch and opened:
        try:
            bars = provider.daily(sorted(watch) + [data.FX_SYMBOL], period="5d"); data.save_bars(bars, now); data.set_source("yfinance-intraday", True, f"{len(bars)} symbols (delayed)")
        except Exception as e: data.set_source("yfinance-intraday", False, str(e)); db.event("ERROR", "monitor", f"intraday refresh failed: {e}", ledger.iso(now))
    for o in pend:
        if (now - datetime.fromisoformat(o["created_ts"])).days >= cfg["pending_expiry_days"]:
            engine.set_order(o["id"], "canceled", "expired unfilled", now); log.append((o["id"], "canceled", "expired")); continue
        if not opened: log.append((o["id"], "blocked", "US market closed")); continue
        if o["needs_approval"] and not o["approved"]: log.append((o["id"], "blocked", "awaiting manual approval")); continue
        today = cal.local_now("US", now).date(); df = data.load_bars(o["symbol"], include_partial=True)
        if len(df) == 0 or df.index[-1].date() != today: log.append((o["id"], "blocked", "no fresh bar for today (data missing/stale) - no trade")); continue
        bar = df.iloc[-1]
        if not (bar.open > 0): log.append((o["id"], "blocked", "invalid open price")); continue
        log.append((o["id"],) + _fill(o, float(bar.open), now, cfg, df))
    # intraday stop checks (positions with stops)
    snap = portfolio.snapshot(now, cfg)
    if opened:
        for h in snap["holdings"]:
            c = db.conn(); ps = c.execute("SELECT * FROM position_state2 WHERE strategy_id=? AND symbol=?", (h["strategy_id"], h["symbol"])).fetchone(); c.close()
            df = data.load_bars(h["symbol"], include_partial=True); today = cal.local_now("US", now).date()
            if not ps or ps["stop"] is None or len(df) == 0 or df.index[-1].date() != today: continue
            bar = df.iloc[-1]
            if bar.low <= ps["stop"]:
                gap = bar.open < ps["stop"]; px = min(float(bar.open), ps["stop"])
                oid = engine.create_order(decision_key=f"STOP:{h['strategy_id']}:{h['symbol']}:{today}", decision_id=None, symbol=h["symbol"], market="US", side="SELL", qty=h["qty"], ref_price=px,
                                          stop=None, target=None, reason=f"stop {ps['stop']:.2f} hit intraday" + (" (gap through stop: filled at open)" if gap else ""), now=now, strategy_id=h["strategy_id"], signal_status="EXIT")
                if oid:
                    o = {"id": oid, "symbol": h["symbol"], "strategy_id": h["strategy_id"], "side": "SELL", "qty": h["qty"], "ref_price": px, "stop": None, "target": None, "decision_id": None, "reason": "stop"}
                    log.append((oid,) + _fill(o, px, now, cfg, df, note="STOP" + (" GAP" if gap else "")))
    if any(r[1] == "filled" for r in log): reconcile(now)
    return log

class _Reject(Exception): pass

def _fill(o, open_px, now, cfg, df, note=""):
    s, sid = o["symbol"], o["strategy_id"]
    adv = float((df["close"] * df["volume"]).iloc[:-1].tail(20).mean()) if len(df) > 20 else None
    try:
        if o["side"] == "BUY":
            if s in cfg["blocked_symbols"]: raise _Reject("symbol manually blocked")
            if cfg["entries_suspended"]: raise _Reject("new entries suspended (drawdown limit)")
            if not cfg.get("automation_on", True): raise _Reject("automation OFF")
            if not C.is_enabled(cfg, sid): raise _Reject(f"strategy {sid} disabled/suspended")
            st = data.data_status(s, now)
            if st["status"] not in ("OK",) and not (st["status"] == "STALE" and False): raise _Reject(f"data {st['status']}: {st['reason']}")
            if o["stop"] is not None:
                if open_px > o["ref_price"] * (1 + cfg["max_gap_chase_pct"] / 100): raise _Reject(f"open {open_px:.2f} gapped >{cfg['max_gap_chase_pct']}% above planned entry {o['ref_price']:.2f}")
                if open_px <= o["stop"]: raise _Reject(f"open {open_px:.2f} at/below stop {o['stop']}")
            fxr = fxm.current(cfg, now)
            if not fxr["ok"]: raise _Reject("FX stale/missing")
            snap = portfolio.snapshot(now, cfg); sl = snap["sleeves"].get(sid)
            if not sl: raise _Reject("no capital allocated to strategy")
            if len({h["symbol"] for h in snap["holdings"]}) >= cfg["max_positions"] and not any(h["symbol"] == s for h in snap["holdings"]): raise _Reject("max positions reached")
            if sid == "baseline":
                qty = o["qty"]; fill, _, _ = risk.exec_price("BUY", open_px, adv, cfg)
                fee = risk.us_fee_breakdown("BUY", fill * qty, qty, cfg)["total"]
                while qty > 0 and fill * qty + fee > sl["buying_power_usd"]: qty -= 1 if not cfg["fractional_shares"] else 0.0001; fee = risk.us_fee_breakdown("BUY", fill * qty, qty, cfg)["total"]
            else:
                reserve = sl["equity_usd"] * cfg["min_cash_pct"] / 100
                sz = risk.size_position_usd(equity_usd=snap["equity_usd"] or sl["equity_usd"], usable_cash_usd=max(0.0, sl["buying_power_usd"] - reserve), price=open_px, stop=o["stop"], cfg=cfg, adv_usd=adv,
                                            sleeve_equity_usd=sl["equity_usd"], sleeve_exposure_usd=sl["positions_usd"])
                qty = min(o["qty"], sz["qty"])
            if qty < (0.0001 if cfg["fractional_shares"] else 1): raise _Reject(f"resized qty {qty} < 1 share after live checks")
            fl = portfolio.fill_us(strategy_id=sid, order_id=o["id"], symbol=s, side="BUY", qty=qty, ref_price=open_px, adv_usd=adv, now=now, note=f"order#{o['id']}", cfg=cfg)
            engine.set_order(o["id"], "filled", f"filled {qty} @ {fl['price']:.4f}", now, fill_price=fl["price"], fill_ts=ledger.iso(now), qty=qty)
            atr = None; d2 = data.load_bars(s)
            if len(d2) > 20: atr = float(scoring.feature_frame(d2).iloc[-1]["atr14"])
            _ps_insert(sid, s, o["stop"], o["target"], atr, now, o.get("decision_id"), o.get("reason"))
            db.event("INFO", "execution", f"[{sid}] FILLED BUY {s} x{qty} @ ${fl['price']:.4f} (fees ${fl['fees_usd']:.3f}, spread ${fl['spread_usd']:.3f}, slippage ${fl['slip_usd']:.3f})", ledger.iso(now))
            return ("filled", f"{qty}@{fl['price']:.4f}")
        pos = {(h["strategy_id"], h["symbol"]): h for h in portfolio.snapshot(now, cfg)["holdings"]}.get((sid, s))
        if not pos: raise _Reject("no position to sell")
        qty = min(o["qty"], pos["qty"])
        fl = portfolio.fill_us(strategy_id=sid, order_id=o["id"], symbol=s, side="SELL", qty=qty, ref_price=open_px, adv_usd=adv, now=now, note=note or f"order#{o['id']}: {o.get('reason','')}"[:200], cfg=cfg)
        engine.set_order(o["id"], "filled", f"filled {qty} @ {fl['price']:.4f}", now, fill_price=fl["price"], fill_ts=ledger.iso(now))
        left = {(h["strategy_id"], h["symbol"]) for h in portfolio.snapshot(now, cfg)["holdings"]}
        if (sid, s) not in left:
            c = db.conn(); c.execute("DELETE FROM position_state2 WHERE strategy_id=? AND symbol=?", (sid, s)); c.commit(); c.close()
        db.event("INFO", "execution", f"[{sid}] FILLED SELL {s} x{qty} @ ${fl['price']:.4f} realized ${fl['realized_usd']:.2f} (settles {fl['settle_date']}) {note}", ledger.iso(now))
        return ("filled", f"{qty}@{fl['price']:.4f}")
    except _Reject as e:
        engine.set_order(o["id"], "rejected", str(e), now); db.event("WARN", "execution", f"order #{o['id']} {s} REJECTED: {e}", ledger.iso(now)); return ("rejected", str(e))
    except Exception as e:
        engine.set_order(o["id"], "rejected", f"error: {e}", now); db.event("ERROR", "execution", f"order #{o['id']} {s} error: {e}", ledger.iso(now)); return ("rejected", str(e))

# ------------------------------------------------------------------ reconcile
def reconcile(now=None):
    now = _now(now); rec = engine.reconcile(now); portfolio.rebuild_curve2(now); return rec
