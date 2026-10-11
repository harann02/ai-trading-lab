"""Append-only ledger: single source of truth for cash, positions, realized P&L. Everything else is derived."""
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
import pandas as pd
from . import db, data, risk, calendars as cal

MYT = ZoneInfo("Asia/Kuala_Lumpur")
def iso(now): return (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(timespec="seconds")
def myt_date(ts: str): return datetime.fromisoformat(ts).astimezone(MYT).date()

def _insert(c, **r):
    cols = ["ts","kind","symbol","market","qty","price_local","ccy","fx","gross_myr","fees_myr","slippage_myr",
            "cash_delta_myr","realized_pnl_myr","order_id","uniq","note"]
    c.execute(f"INSERT INTO ledger({','.join(cols)}) VALUES({','.join('?'*len(cols))})", [r.get(k) for k in cols])

def init_account(now=None, capital=None):
    """Credits starting capital exactly once (unique key INITIAL)."""
    cfg = db.get_settings(); amt = capital if capital is not None else cfg["starting_capital"]
    c = db.conn()
    try:
        _insert(c, ts=iso(now), kind="DEPOSIT", ccy="MYR", gross_myr=amt, fees_myr=0, cash_delta_myr=amt, uniq="INITIAL", note="Initial virtual capital (paper)")
        c.commit(); created = True
    except Exception: created = False
    c.close(); return created

def cash_flow(kind, amount, now=None, note=""):
    assert kind in ("DEPOSIT", "WITHDRAW") and amount > 0
    if kind == "WITHDRAW" and amount > cash() + 1e-9: raise ValueError("insufficient cash for withdrawal")
    c = db.conn()
    _insert(c, ts=iso(now), kind=kind, ccy="MYR", gross_myr=amount, fees_myr=0, cash_delta_myr=amount if kind == "DEPOSIT" else -amount, note=note or "manual")
    c.commit(); c.close()

def rows(asof_ts=None):
    c = db.conn()
    r = c.execute("SELECT * FROM ledger" + (" WHERE ts<=?" if asof_ts else "") + " ORDER BY id", (asof_ts,) if asof_ts else ()).fetchall(); c.close()
    return [dict(x) for x in r]

def cash(asof_ts=None) -> float:
    return sum(r["cash_delta_myr"] for r in rows(asof_ts))

def replay(rws):
    """Returns (positions dict, closed trades list). Average-cost method in MYR (buy fees capitalised)."""
    pos, trades = {}, []
    for r in rws:
        if r.get("strategy_id"): continue          # v2 strategy/USD rows are replayed by portfolio.replay2 (separate cost basis in USD)
        if r["kind"] == "BUY":
            p = pos.setdefault(r["symbol"], {"symbol": r["symbol"], "market": r["market"], "qty": 0, "cost_myr": 0.0, "first_ts": r["ts"], "fees": 0.0, "slip": 0.0, "ccy": r["ccy"]})
            p["qty"] += r["qty"]; p["cost_myr"] += r["gross_myr"] + r["fees_myr"]; p["fees"] += r["fees_myr"]; p["slip"] += r["slippage_myr"] or 0
        elif r["kind"] == "SELL":
            p = pos[r["symbol"]]; avg = p["cost_myr"] / p["qty"]
            trades.append({"symbol": r["symbol"], "market": r["market"], "entry_ts": p["first_ts"], "exit_ts": r["ts"], "qty": r["qty"],
                           "avg_cost_myr": avg, "proceeds_myr": r["gross_myr"] - r["fees_myr"], "pnl_myr": r["realized_pnl_myr"],
                           "ret": r["realized_pnl_myr"] / (avg * r["qty"]), "fees_myr": r["fees_myr"] + p["fees"] * r["qty"] / p["qty"],
                           "slip_myr": (r["slippage_myr"] or 0) + p["slip"] * r["qty"] / p["qty"], "note": r["note"], "order_id": r["order_id"]})
            p["cost_myr"] -= avg * r["qty"]; p["qty"] -= r["qty"]
            p["fees"] -= p["fees"] * r["qty"] / (p["qty"] + r["qty"]); p["slip"] -= p["slip"] * r["qty"] / (p["qty"] + r["qty"])
            if p["qty"] == 0: del pos[r["symbol"]]
    return pos, trades

def positions(asof_ts=None): return replay(rows(asof_ts))[0]
def closed_trades(): return replay(rows())[1]

def _px(sym, now, include_partial=True, end=None):
    df = data.load_bars(sym, include_partial=include_partial, end=end)
    return (None if len(df) == 0 else (float(df["close"].iloc[-1]), df.index[-1].date(), bool(df["final"].iloc[-1]))), df

def mark_positions(now=None, pos=None) -> list:
    """Holdings marked to market in MYR using latest bar (flags stale per data_status)."""
    now = now or datetime.now(timezone.utc); pos = positions() if pos is None else pos; out = []
    fxr = data.get_fx(now)
    for s, p in pos.items():
        px, df = _px(s, now)
        st = data.data_status(s, now) if px else {"status": "MISSING"}
        fx = 1.0 if p["market"] == "BURSA" else (fxr["rate"] or None)
        if px is None or fx is None: val, last, flag = p["cost_myr"], None, "NO PRICE: valued at cost (flagged)"
        else:
            last = px[0]; val = last * fx * p["qty"]
            flag = "" if st["status"] == "OK" else f"{st['status']}: {st.get('reason', '')}"
        out.append({**p, "last_price": last, "last_bar": str(px[1]) if px else None, "fx": fx, "market_value_myr": val,
                    "unrealized_pnl_myr": val - p["cost_myr"], "unrealized_pct": (val / p["cost_myr"] - 1) if p["cost_myr"] else 0,
                    "data_flag": flag})
    return out

def equity_now(now=None) -> dict:
    now = now or datetime.now(timezone.utc); h = mark_positions(now); c = cash()
    mv = sum(x["market_value_myr"] for x in h)
    from . import portfolio
    v2 = portfolio.usd_assets_myr(now)               # USD cash + USD holdings of all strategy sleeves, in MYR at mid FX (0 if no v2 rows)
    mv += v2["positions_myr"]; c += v2["cash_myr"]
    return {"cash": c, "positions_value": mv, "equity": c + mv, "exposure_pct": (mv / (c + mv) * 100) if c + mv else 0,
            "holdings": h, "stale": [x["symbol"] for x in h if x["data_flag"]]}

def state_asof(d, allr=None):
    """Deterministic portfolio state at end of MYT date d from ledger + cached bars. Returns dict."""
    allr = rows() if allr is None else allr
    cutoff_ts = datetime(d.year, d.month, d.day, 23, 59, 59, tzinfo=MYT).astimezone(timezone.utc).isoformat(timespec="seconds")
    sub = [r for r in allr if r["ts"] <= cutoff_ts]
    pos, _ = replay(sub); cs = sum(r["cash_delta_myr"] for r in sub)
    from . import portfolio
    v2 = portfolio.usd_assets_asof(sub, d); cs += v2["cash_myr"]
    flows = sum(r["cash_delta_myr"] for r in allr if r["kind"] in ("DEPOSIT", "WITHDRAW") and myt_date(r["ts"]) == d)
    mv, stale, hold = 0.0, [], []
    for s, p in pos.items():
        df = data.load_bars(s, include_partial=False, end=d); fx = 1.0
        if p["market"] == "US": fx = data.fx_on(d) or 0
        if len(df) == 0 or not fx:
            v = p["cost_myr"]; stale.append(s); px = None
        else:
            px = float(df["close"].iloc[-1]); v = px * fx * p["qty"]
            if (pd.Timestamp(d) - df.index[-1]).days > 5: stale.append(s)
        mv += v; hold.append({"symbol": s, "qty": p["qty"], "cost_myr": round(p["cost_myr"], 4), "last_close": px, "value_myr": round(v, 4)})
    mv += v2["positions_myr"]; stale += v2["stale"]
    return {"cash": cs, "positions_value": mv, "equity": cs + mv, "flows": flows, "stale": stale, "holdings": hold, "trades": [r for r in sub if myt_date(r["ts"]) == d and r["kind"] in ("BUY", "SELL")]}

def rebuild_equity_curve(now=None):
    """Derived table: recompute daily equity from the ledger and cached closes (deterministic)."""
    now = now or datetime.now(timezone.utc); allr = rows()
    if not allr: return []
    start = myt_date(allr[0]["ts"]); end = now.astimezone(MYT).date()
    out = []; d = start
    while d <= end:
        if d.weekday() < 5 or d == end or d == start:
            st = state_asof(d, allr)
            out.append((str(d), st["equity"], st["cash"], st["positions_value"], st["flows"], ",".join(st["stale"]), None))
        d += timedelta(days=1)
    c = db.conn(); c.execute("DELETE FROM equity_curve")
    c.executemany("INSERT INTO equity_curve VALUES(?,?,?,?,?,?,?)", out); c.commit(); c.close()
    return out

def execute_fill(*, order_id, symbol, market, side, qty, raw_price, now, note="", cfg=None):
    """Writes the BUY/SELL ledger row. Applies slippage + fees + FX. Idempotent per (order, side)."""
    cfg = cfg or db.get_settings(); fxr = data.get_fx(now)
    fx = 1.0 if market == "BURSA" else fxr["rate"]
    if fx is None or (market == "US" and not fxr["ok"]): raise RuntimeError("FX unavailable/stale: cannot convert USD")
    px = risk.slip_price(market, side, raw_price, cfg); value = px * qty
    fee_loc = risk.fees_local(market, side, value, qty, cfg)
    fee = fee_loc * fx + (value * fx * cfg["fx_spread_bps"] / 1e4 if market == "US" else 0)
    gross = value * fx; slip = abs(px - raw_price) * qty * fx
    ccy = "MYR" if market == "BURSA" else "USD"
    c = db.conn()
    try:
        c.execute("BEGIN IMMEDIATE")
        rws = [dict(r) for r in c.execute("SELECT * FROM ledger ORDER BY id")]
        pos, _ = replay(rws); cs = sum(r["cash_delta_myr"] for r in rws)
        if side == "BUY":
            if cs < gross + fee - 1e-9: raise ValueError("insufficient cash")
            _insert(c, ts=iso(now), kind="BUY", symbol=symbol, market=market, qty=qty, price_local=px, ccy=ccy, fx=fx, gross_myr=gross,
                    fees_myr=fee, slippage_myr=slip, cash_delta_myr=-(gross + fee), order_id=order_id, uniq=f"fill:{order_id}:BUY", note=note)
            realized = None
        else:
            p = pos.get(symbol)
            if not p or p["qty"] < qty: raise ValueError("cannot sell more than held (no shorting)")
            realized = gross - fee - p["cost_myr"] / p["qty"] * qty
            _insert(c, ts=iso(now), kind="SELL", symbol=symbol, market=market, qty=qty, price_local=px, ccy=ccy, fx=fx, gross_myr=gross,
                    fees_myr=fee, slippage_myr=slip, cash_delta_myr=gross - fee, realized_pnl_myr=realized, order_id=order_id,
                    uniq=f"fill:{order_id}:SELL", note=note)
        c.commit()
    except Exception:
        c.rollback(); c.close(); raise
    c.close()
    return {"price": px, "fx": fx, "fees_myr": fee, "slippage_myr": slip, "gross_myr": gross, "realized_pnl_myr": realized}
