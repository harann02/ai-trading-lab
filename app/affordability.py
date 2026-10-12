"""Small-account AFFORDABILITY gate (P7). Evaluates a candidate against what the account can ACTUALLY do: whole-share price vs the single-stock cap, 1-share minimum,
ATR-stop risk per share vs the 1% risk budget, usable cash after the 20% reserve, exposure room, liquidity. It never relaxes a limit: it only EXPLAINS (reason code) and COUNTS.
Cash is a legitimate outcome: 'no affordable valid trade' is a decision, not an error."""
import json
from datetime import datetime, timezone
from . import db

CODES = {"AFFORDABLE": "affordable at whole-share size",
         "UNAFFORDABLE_PRICE_GT_CAP": "one share (incl. spread/fees) costs more than the single-stock cap",
         "UNAFFORDABLE_RISK_PER_SHARE": "loss per share to the ATR stop (incl. friction) exceeds the 1% risk budget, so even 1 share breaks the risk rule",
         "UNAFFORDABLE_CASH": "one share costs more than usable cash after the cash reserve",
         "UNAFFORDABLE_EXPOSURE": "no exposure room left for one share",
         "UNAFFORDABLE_LIQUIDITY": "one share exceeds the liquidity (ADV%) cap",
         "UNAFFORDABLE_INVALID_STOP": "no valid stop below the entry"}
BINDING_TO_CODE = {"risk": "UNAFFORDABLE_RISK_PER_SHARE", "single_stock_pct": "UNAFFORDABLE_PRICE_GT_CAP", "affordability": "UNAFFORDABLE_CASH",
                   "sleeve_exposure": "UNAFFORDABLE_EXPOSURE", "liquidity": "UNAFFORDABLE_LIQUIDITY", "invalid_stop": "UNAFFORDABLE_INVALID_STOP", "no_allocation": "UNAFFORDABLE_CASH"}

def classify(sz, min_qty=1):
    """sz = risk.size_position_usd() result. Returns (code, detail dict with the numbers behind it)."""
    d = sz.get("detail") or {}; caps = d.get("caps") or {}
    out = {"qty": sz.get("qty"), "binding": sz.get("binding"), "caps": caps, "fill_price_usd": d.get("fill_price_usd"), "loss_per_share_usd": d.get("loss_per_share_usd"), "risk_budget_usd": d.get("risk_budget_usd")}
    if (sz.get("qty") or 0) >= min_qty: return "AFFORDABLE", out
    if sz.get("binding") == "invalid_stop": out["all_reasons"] = ["UNAFFORDABLE_INVALID_STOP"]; return "UNAFFORDABLE_INVALID_STOP", out
    # every cap that blocks a single share, in priority order (structural reasons first; ties are not hidden)
    order = [("single_stock_pct", "UNAFFORDABLE_PRICE_GT_CAP"), ("affordability", "UNAFFORDABLE_CASH"), ("sleeve_exposure", "UNAFFORDABLE_EXPOSURE"), ("liquidity", "UNAFFORDABLE_LIQUIDITY"), ("risk", "UNAFFORDABLE_RISK_PER_SHARE")]
    why = [code for k, code in order if k in caps and caps[k] < min_qty]
    out["all_reasons"] = why or [BINDING_TO_CODE.get(sz.get("binding"), "UNAFFORDABLE_CASH")]
    return out["all_reasons"][0], out

def explain(code, det, cap_usd=None):
    f = det.get("fill_price_usd"); l = det.get("loss_per_share_usd"); b = det.get("risk_budget_usd")
    bits = [code]; extra = [r for r in (det.get("all_reasons") or []) if r != code]
    if code == "UNAFFORDABLE_PRICE_GT_CAP" and f is not None and cap_usd is not None: bits.append(f"1 share ≈ ${f:,.2f} > single-stock cap ${cap_usd:,.2f}")
    elif code == "UNAFFORDABLE_RISK_PER_SHARE" and l is not None and b is not None: bits.append(f"risk/share ${l:,.2f} > 1% risk budget ${b:,.2f}")
    elif code != "AFFORDABLE": bits.append(CODES.get(code, ""))
    else: bits.append(f"qty {det.get('qty')}")
    return ": ".join(bits) + (f" (also: {', '.join(extra)})" if extra else "")

def ensure_table(c):
    c.execute("CREATE TABLE IF NOT EXISTS affordability_daily(scan_date TEXT, strategy_id TEXT, ts TEXT, equity_usd REAL, cap_usd REAL, risk_budget_usd REAL, evaluated INTEGER, affordable INTEGER, fractional_affordable INTEGER, by_code TEXT, PRIMARY KEY(scan_date, strategy_id))")

def record_daily(scan_date, strategy_id, equity_usd, cap_usd, risk_budget_usd, codes, frac_affordable=None, now=None):
    """codes: list of reason codes, one per evaluated candidate. Idempotent per (scan_date, strategy)."""
    by = {}
    for k in codes: by[k] = by.get(k, 0) + 1
    c = db.conn(); ensure_table(c)
    c.execute("INSERT OR REPLACE INTO affordability_daily VALUES(?,?,?,?,?,?,?,?,?,?)", (str(scan_date), strategy_id, (now or datetime.now(timezone.utc)).isoformat(timespec="seconds"), equity_usd, cap_usd, risk_budget_usd,
              len(codes), by.get("AFFORDABLE", 0), frac_affordable, json.dumps(by, sort_keys=True))); c.commit(); c.close(); return by

def latest(n=10):
    try:
        c = db.conn(); ensure_table(c); r = [dict(x) for x in c.execute("SELECT * FROM affordability_daily ORDER BY scan_date DESC LIMIT ?", (n,))]; c.close()
        for x in r: x["by_code"] = json.loads(x["by_code"] or "{}")
        return r
    except Exception: return []
