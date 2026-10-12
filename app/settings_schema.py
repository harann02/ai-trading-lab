"""Validated settings. ONE validator used by the FastAPI backend and by the GitHub-control applier (engine re-validates everything server-side).
Only whitelisted keys are owner-writable; risk-engine owned keys (entries_suspended, suspend_reason, dd_baseline_ts) are never settable here."""
import re
TICKER = re.compile(r"^[A-Z]{1,5}([.-][A-Z])?$")
STRATS = ("swing", "meanrev", "baseline", "intraday")

def _num(lo, hi, integer=False, lo_open=False):
    def v(x):
        if isinstance(x, bool) or not isinstance(x, (int, float)): raise ValueError("must be a number")
        if integer and int(x) != x: raise ValueError("must be an integer")
        if (x <= lo if lo_open else x < lo) or x > hi: raise ValueError(f"must be in {'(' if lo_open else '['}{lo}, {hi}]")
        return int(x) if integer else float(x)
    return v
def _bool(x):
    if not isinstance(x, bool): raise ValueError("must be true/false")
    return x
def _tickers(x):
    if not isinstance(x, list) or len(x) > 60: raise ValueError("must be a list of <=60 tickers")
    out = []
    for t in x:
        t = str(t).strip().upper()
        if not TICKER.match(t): raise ValueError(f"bad ticker {t!r}")
        out.append(t)
    return sorted(set(out))

SCHEMA = {
    "risk_per_trade_pct": _num(0, 5, lo_open=True), "max_position_pct": _num(0, 100, lo_open=True), "max_exposure_pct": _num(0, 100, lo_open=True),
    "min_cash_pct": _num(0, 100), "max_positions": _num(1, 10, integer=True), "max_drawdown_pct": _num(0, 50, lo_open=True), "atr_stop_mult": _num(1, 8),
    "fx_spread_bps": _num(0, 200), "automation_on": _bool, "auto_trade": _bool, "manual_approval": _bool, "fractional_shares": _bool, "cash_account_mode": _bool,
    "unsettled_proceeds_reusable": _bool, "weekly_entries_only": _bool, "buy_min_score": _num(50, 100), "sideways_min_score": _num(50, 100), "min_rr": _num(0, 10),
    "extra_watchlist": _tickers, "blocked_symbols": _tickers, "baseline_symbols": _tickers,
    "entry_cadence": lambda x: x if x in ("weekly", "daily", "breakout_daily") else (_ for _ in ()).throw(ValueError("must be weekly | daily | breakout_daily")),
    "fill_delay_min": _num(0, 120), "max_price_age_min": _num(5, 240), "stop_extra_slippage_bps": _num(0, 200),
}
RETIRED_ZERO = ("meanrev", "baseline", "intraday")   # v3: allocation stays 0 unless the owner reactivates through the validated reactivate_strategy command

def validate_patch(patch: dict, current: dict):
    """Returns (clean_patch, errors{key:msg}). Strategy toggles/allocations come as patch['strategies'] = {sid: {enabled, allocation_pct}} and are merged."""
    clean, errs = {}, {}
    for k, v in (patch or {}).items():
        if k == "strategies":
            cur = {s: dict(x) for s, x in current["strategies"].items()}
            if not isinstance(v, dict): errs[k] = "must be an object"; continue
            for sid, sv in v.items():
                if sid not in STRATS: errs[f"strategies.{sid}"] = "unknown strategy"; continue
                for kk, vv in (sv or {}).items():
                    try:
                        if kk == "enabled": cur[sid]["enabled"] = _bool(vv)
                        elif kk == "allocation_pct":
                            cur[sid]["allocation_pct"] = _num(0, 100)(vv)
                            if sid in RETIRED_ZERO and cur[sid]["allocation_pct"] > 0:
                                from .v3 import policy as _p
                                if not _p.new_entries_allowed(sid): raise ValueError(f"{sid} is research-only/retired in v3: allocation must stay 0% (reactivation needs the validated owner command)")
                        else: errs[f"strategies.{sid}.{kk}"] = "not writable"
                    except ValueError as e: errs[f"strategies.{sid}.{kk}"] = str(e)
            if sum(x["allocation_pct"] for x in cur.values()) > 100.0 + 1e-9: errs["strategies"] = "allocations sum to more than 100% (no double allocation)"
            if "strategies" not in errs and not any(e.startswith("strategies.") for e in errs): clean["strategies"] = cur
        elif k in SCHEMA:
            try: clean[k] = SCHEMA[k](v)
            except ValueError as e: errs[k] = str(e)
        elif k in ("entries_suspended", "suspend_reason", "dd_baseline_ts"): errs[k] = "controlled by the risk engine (use the resume command)"
        else: errs[k] = "unknown or not owner-writable"
    return clean, errs
