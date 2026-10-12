"""v2 portfolio accounting: MYR treasury + per-strategy USD sleeves, derived ONLY from the append-only ledger (+ cached bars + recorded FX).
Rows with strategy_id are v2 rows. Cost basis is USD average cost; the FX effect is isolated at sleeve level:
  value_myr - invested_myr = (V_usd - U_usd) * basis  [stock/trading result incl. costs]  +  V_usd * (fx_now - basis)  [currency gain/loss]
with basis = invested_myr / usd_received (effective rate paid incl. the FX spread). Exact identity, tested."""
import json
from datetime import datetime, timezone, date
from . import db, ledger, data, calendars as cal, risk

def _r(x, n=6): return None if x is None else round(float(x), n)

def replay2(rows):
    """Returns dict with positions {(sid,sym)}, trades [..], sleeves {sid: {...cash,usd_received,invested_myr..}}."""
    pos, trades, sl = {}, [], {}
    def S(sid): return sl.setdefault(sid, {"cash_usd": 0.0, "invested_myr": 0.0, "usd_received": 0.0, "fees_usd": 0.0, "spread_usd": 0.0, "slip_usd": 0.0,
                                           "fx_spread_myr": 0.0, "realized_usd": 0.0, "unsettled_rows": []})
    for r in rows:
        sid = r.get("strategy_id")
        if not sid: continue
        s = S(sid); s["cash_usd"] += r.get("cash_delta_usd") or 0.0
        k = r["kind"]
        if k == "FX_CONVERT":
            s["invested_myr"] += r["gross_myr"]; s["usd_received"] += r["gross_usd"]; s["fx_spread_myr"] += r["fees_myr"] or 0.0
        elif k in ("SLEEVE_XFER_OUT", "SLEEVE_XFER_IN"):
            # v3 internal journal entry: moves USD cash + its MYR cost basis (and the FX spread already paid) between sleeves. Not a deposit, no FX, no P&L.
            sg = -1 if k == "SLEEVE_XFER_OUT" else 1
            try: carried = float((json.loads(r.get("fee_detail") or "{}")).get("fx_spread_myr_carried", 0.0))
            except Exception: carried = 0.0
            s["invested_myr"] += sg * r["gross_myr"]; s["usd_received"] += sg * r["gross_usd"]; s["fx_spread_myr"] += sg * carried
        elif k == "BUY":
            p = pos.setdefault((sid, r["symbol"]), {"strategy_id": sid, "symbol": r["symbol"], "qty": 0.0, "cost_usd": 0.0, "first_ts": r["ts"], "fees_usd": 0.0, "spread_usd": 0.0, "slip_usd": 0.0})
            p["qty"] += r["qty"]; p["cost_usd"] += r["gross_usd"] + (r["fees_usd"] or 0)
            for a in ("fees_usd", "spread_usd", "slip_usd"): p[a] += r.get(a) or 0; s[a] += r.get(a) or 0
        elif k == "SELL":
            p = pos[(sid, r["symbol"])]; avg = p["cost_usd"] / p["qty"]; frac = r["qty"] / p["qty"]
            basis = (s["invested_myr"] / s["usd_received"]) if s["usd_received"] else 0
            pnl_usd = r["gross_usd"] - (r["fees_usd"] or 0) - avg * r["qty"]
            trades.append({"strategy_id": sid, "symbol": r["symbol"], "entry_ts": p["first_ts"], "exit_ts": r["ts"], "qty": r["qty"], "avg_cost_usd": avg,
                           "proceeds_usd": r["gross_usd"] - (r["fees_usd"] or 0), "pnl_usd": pnl_usd, "pnl_myr": pnl_usd * basis, "ret": pnl_usd / (avg * r["qty"]),
                           "fees_usd": (r["fees_usd"] or 0) + p["fees_usd"] * frac, "cost_usd_spread_slip": (r.get("spread_usd") or 0) + (r.get("slip_usd") or 0) + (p["spread_usd"] + p["slip_usd"]) * frac,
                           "note": r["note"], "order_id": r["order_id"]})
            for a in ("fees_usd", "spread_usd", "slip_usd"): s[a] += r.get(a) or 0; p[a] -= p[a] * frac
            p["cost_usd"] -= avg * r["qty"]; p["qty"] -= r["qty"]; s["realized_usd"] += pnl_usd
            if p["qty"] <= 1e-9: del pos[(sid, r["symbol"])]
            s["unsettled_rows"].append((r["settle_date"], r["cash_delta_usd"]))
    return {"positions": pos, "trades": trades, "sleeves": sl}

def _unsettled(sleeve, today_ny: date):
    return sum(a for sd, a in sleeve["unsettled_rows"] if sd and sd > str(today_ny))

def _price(sym, end=None, include_partial=True):
    df = data.load_bars(sym, include_partial=include_partial, end=end)
    return (float(df["close"].iloc[-1]), str(df.index[-1].date())) if len(df) else (None, None)

def fx_mid_on(d):
    """USD/MYR mid for date d: FX bar <= d, else stored fx_rates <= d, else the mid used by the earliest recorded conversion. Never 0 (a 0 rate would fake a 100% drawdown)."""
    v = data.fx_on(d)
    if v: return v
    c = db.conn(); r = c.execute("SELECT rate FROM fx_rates WHERE date<=? ORDER BY date DESC LIMIT 1", (str(d)[:10],)).fetchone()
    if not r: r = c.execute("SELECT mid_rate AS rate FROM fx_conversions ORDER BY id LIMIT 1").fetchone()
    if not r: r = c.execute("SELECT rate FROM fx_rates ORDER BY date LIMIT 1").fetchone()
    c.close(); return float(r["rate"]) if r else None

def usd_assets_asof(rows, d: date):
    """USD sleeves valued in MYR at end of date d (final bars, fx_on(d)). Used by the equity curve."""
    rp = replay2(rows)
    if not rp["sleeves"]: return {"cash_myr": 0.0, "positions_myr": 0.0, "stale": []}
    fx = fx_mid_on(d) or 0.0; cash = sum(s["cash_usd"] for s in rp["sleeves"].values()); mv = 0.0; stale = []
    for (sid, sym), p in rp["positions"].items():
        px, pd_ = _price(sym, end=d, include_partial=False)
        if px is None: mv += p["cost_usd"]; stale.append(sym)
        else:
            mv += px * p["qty"]
            if (d - date.fromisoformat(pd_)).days > 5: stale.append(sym)
    return {"cash_myr": cash * fx, "positions_myr": mv * fx, "stale": stale, "fx": fx}

def usd_assets_myr(now=None):
    now = now or datetime.now(timezone.utc)
    rows = [r for r in ledger.rows() if r.get("strategy_id")]
    if not rows: return {"cash_myr": 0.0, "positions_myr": 0.0}
    from . import fx as fxm
    fx = (fxm.current(None, now) or {}).get("rate") or 0.0; rp = replay2(rows)
    cash = sum(s["cash_usd"] for s in rp["sleeves"].values()); mv = 0.0
    for (sid, sym), p in rp["positions"].items():
        px, _ = _price(sym); mv += (px if px is not None else p["cost_usd"] / p["qty"]) * p["qty"]
    return {"cash_myr": cash * fx, "positions_myr": mv * fx}

def snapshot(now=None, cfg=None) -> dict:
    """Full live view: per-strategy sleeves (USD & MYR), holdings, settled/unsettled cash, buying power, FX attribution."""
    from . import fx as fxm
    now = now or datetime.now(timezone.utc); cfg = cfg or db.get_settings(); rows = ledger.rows()
    fxr = fxm.current(cfg, now); fx = fxr.get("rate") or 0.0
    today_ny = now.astimezone(cal.NY).date(); rp = replay2(rows)
    cash_myr = sum(r["cash_delta_myr"] for r in rows)
    holdings, sleeves = [], {}
    for (sid, sym), p in sorted(rp["positions"].items()):
        px, bd = _price(sym); val = (px if px is not None else p["cost_usd"] / p["qty"]) * p["qty"]
        holdings.append({"strategy_id": sid, "symbol": sym, "qty": p["qty"], "avg_cost_usd": p["cost_usd"] / p["qty"], "last_price_usd": px, "last_bar": bd,
                         "value_usd": val, "value_myr": val * fx, "cost_usd": p["cost_usd"], "unrealized_usd": val - p["cost_usd"], "entry_ts": p["first_ts"],
                         "data_flag": "" if px is not None else "NO PRICE: valued at cost"})
    for sid, s in rp["sleeves"].items():
        hv = sum(h["value_usd"] for h in holdings if h["strategy_id"] == sid)
        unsett = _unsettled(s, today_ny); settled = s["cash_usd"] - unsett
        eq_usd = s["cash_usd"] + hv; basis = (s["invested_myr"] / s["usd_received"]) if s["usd_received"] else None
        bp = settled if (cfg["cash_account_mode"] and not cfg["unsettled_proceeds_reusable"]) else s["cash_usd"]
        eq_myr = eq_usd * fx
        stock_myr = (eq_usd - s["usd_received"]) * basis if basis else 0.0
        fx_myr = eq_usd * (fx - basis) if basis else 0.0
        sleeves[sid] = {"strategy_id": sid, "invested_myr": s["invested_myr"], "usd_received": s["usd_received"], "fx_basis": basis, "cash_usd": s["cash_usd"], "settled_cash_usd": settled,
                        "unsettled_cash_usd": unsett, "buying_power_usd": max(0.0, bp), "positions_usd": hv, "equity_usd": eq_usd, "equity_myr": eq_myr,
                        "total_pnl_myr": eq_myr - s["invested_myr"], "stock_pnl_usd": eq_usd - s["usd_received"], "stock_pnl_myr": stock_myr, "currency_pnl_myr": fx_myr,
                        "fx_spread_paid_myr": s["fx_spread_myr"], "fees_usd": s["fees_usd"], "spread_usd": s["spread_usd"], "slip_usd": s["slip_usd"],
                        "realized_usd": s["realized_usd"], "return_on_allocated_pct": ((eq_myr / s["invested_myr"] - 1) * 100) if s["invested_myr"] else None}
    usd_eq_myr = sum(x["equity_myr"] for x in sleeves.values())
    return {"fx": fxr, "cash_myr": cash_myr, "sleeves": sleeves, "holdings": holdings, "usd_equity_myr": usd_eq_myr, "equity_myr": cash_myr + usd_eq_myr,
            "equity_usd": (cash_myr + usd_eq_myr) / fx if fx else None, "trades": rp["trades"], "today_ny": str(today_ny)}

def fill_us(*, strategy_id, order_id, symbol, side, qty, ref_price, adv_usd, now, note="", cfg=None):
    """Writes a USD BUY/SELL ledger row atomically: execution price = ref +/- (modelled half-spread + slippage), explicit SEC/TAF/commission fees.
    BUY needs SETTLED cash in cash-account mode; SELL creates unsettled proceeds (T+1 NYSE sessions). No shorting. Idempotent per (order, side)."""
    cfg = cfg or db.get_settings()
    px, spr_ps, slp_ps = risk.exec_price(side, ref_price, adv_usd, cfg); value = px * qty
    fee = risk.us_fee_breakdown(side, value, qty, cfg); today_ny = now.astimezone(cal.NY).date()
    settle = str(cal.add_sessions(today_ny, int(cfg["settlement_days"]))) if side == "SELL" else str(today_ny)
    uniq = f"fill:{order_id}:{side}"
    c = db.conn()
    try:
        c.execute("BEGIN IMMEDIATE")
        rws = [dict(r) for r in c.execute("SELECT * FROM ledger WHERE strategy_id IS NOT NULL ORDER BY id")]
        rp = replay2(rws); s = rp["sleeves"].get(strategy_id)
        if not s: raise ValueError("strategy has no USD allocation")
        basis = (s["invested_myr"] / s["usd_received"]) if s["usd_received"] else None
        if side == "BUY":
            need = value + fee["total"]
            avail = s["cash_usd"] - _unsettled(s, today_ny) if (cfg["cash_account_mode"] and not cfg["unsettled_proceeds_reusable"]) else s["cash_usd"]
            if avail < need - 1e-9: raise ValueError(f"insufficient {'settled ' if cfg['cash_account_mode'] else ''}USD cash: {avail:.2f} < {need:.2f}")
            realized = None; delta = -need
            cols = dict(kind="BUY", cash_delta_usd=delta)
        else:
            p = rp["positions"].get((strategy_id, symbol))
            if not p or p["qty"] + 1e-9 < qty: raise ValueError("cannot sell more than held (no shorting)")
            realized = value - fee["total"] - p["cost_usd"] / p["qty"] * qty; delta = value - fee["total"]
        c.execute("INSERT INTO ledger(ts,kind,symbol,market,qty,price_local,ccy,fx,gross_myr,fees_myr,slippage_myr,cash_delta_myr,realized_pnl_myr,order_id,uniq,note,strategy_id,cash_delta_usd,gross_usd,fees_usd,spread_usd,slip_usd,fx_basis,settle_date,realized_stock_usd,fee_detail) "
                  "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (ledger.iso(now), side, symbol, "US", qty, px, "USD", basis, 0.0, 0.0, 0.0, 0.0, (realized * basis if (realized is not None and basis) else None), order_id, uniq, note, strategy_id,
                   delta, value, fee["total"], spr_ps * qty, slp_ps * qty, basis, settle, realized, json.dumps(fee)))
        c.commit()
    except Exception: c.rollback(); c.close(); raise
    c.close()
    return {"price": px, "gross_usd": value, "fees_usd": fee["total"], "fee_detail": fee, "spread_usd": spr_ps * qty, "slip_usd": slp_ps * qty, "realized_usd": realized, "settle_date": settle}

def rebuild_curve2(now=None):
    """Derived table equity_curve2: per-day, per-sleeve equity (USD and MYR at that day's mid FX) from ledger + final bars. Deterministic. Also 'ALL' (incl. MYR treasury)."""
    from datetime import timedelta
    now = now or datetime.now(timezone.utc); allr = ledger.rows(); rows = [r for r in allr if r.get("strategy_id")]
    if not rows: return []
    start = ledger.myt_date(rows[0]["ts"]); end = now.astimezone(ledger.MYT).date(); out = []; d = start
    while d <= end:
        if d.weekday() < 5 or d == end or d == start:
            cutoff = datetime(d.year, d.month, d.day, 23, 59, 59, tzinfo=ledger.MYT).astimezone(timezone.utc).isoformat(timespec="seconds")
            sub = [r for r in allr if r["ts"] <= cutoff]; v2 = [r for r in sub if r.get("strategy_id")]; rp = replay2(v2)
            fx = fx_mid_on(d) or 0.0; tot_usd = 0.0; tot_myr = sum(r["cash_delta_myr"] for r in sub)
            for sid, s in rp["sleeves"].items():
                hv = 0.0
                for (psid, sym), p in rp["positions"].items():
                    if psid != sid: continue
                    px, _ = _price(sym, end=d, include_partial=False); hv += (px * p["qty"]) if px is not None else p["cost_usd"]
                eq = s["cash_usd"] + hv; tot_usd += eq
                out.append((str(d), sid, eq * fx, eq, s["cash_usd"], hv, fx))
            out.append((str(d), "ALL", tot_myr + tot_usd * fx, tot_usd, None, None, fx))
        d += timedelta(days=1)
    c = db.conn(); c.execute("DELETE FROM equity_curve2"); c.executemany("INSERT INTO equity_curve2 VALUES(?,?,?,?,?,?,?)", out); c.commit(); c.close()
    return out
